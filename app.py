import os
import uuid
import threading
import subprocess
import requests
import textwrap
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from PIL import Image, ImageDraw, ImageFont
from pydub import AudioSegment
import cloudinary
import cloudinary.uploader

load_dotenv()

app = Flask(__name__)

# CONFIGURACIÓN DE CLOUDINARY: valores tomados de variables de entorno (ver .env.example)
cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME"),
    api_key=os.environ.get("CLOUDINARY_API_KEY"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET"),
    secure=True
)

# Estado de los jobs en memoria (por ahora). Clave: job_id -> dict de estado.
# Protegido con un lock porque lo escribe el hilo de fondo y lo lee el request.
jobs = {}
jobs_lock = threading.Lock()

# Limita a 1 solo render de video en simultáneo. Render Starter tiene apenas
# 512MB de RAM — si Make dispara varios POST /fabricar seguidos (reintentos,
# o corridas manuales encimadas), sin este límite cada uno arranca su propio
# proceso de ffmpeg al mismo tiempo y se quedan sin memoria entre todos,
# tirando el servidor entero abajo (esto ya pasó una vez). Con el semáforo,
# los jobs de más simplemente esperan su turno en vez de competir por RAM.
semaforo_render = threading.Semaphore(1)

# Acumulador para el patrón "una URL por request, dispara el render en la
# última". Reemplaza al Aggregator de Make (que tenía un bug de plataforma
# confirmado — no cerraba el ciclo del Iterator, documentado en el foro
# oficial de Make por otros usuarios con el mismo patrón). Clave: job_key
# (usamos el número de fila del Sheet) -> dict con las URLs por posición y
# los demás datos del job.
acumuladores = {}
acumuladores_lock = threading.Lock()

# --- Estilo visual de la miniatura ---------------------------------------
# Franja azul muy oscuro / texto dorado-ámbar: mantiene la identidad calmada
# del canal (en vez del amarillo/negro genérico de muchos canales
# automatizados). Fácil de cambiar a amarillo/negro si un Test & Compare
# real en YouTube muestra que convierte más.
COLOR_FRANJA = (10, 25, 40)       # azul muy oscuro, casi negro-azulado
COLOR_TEXTO = (230, 180, 90)      # dorado / ámbar suave
COLOR_VERDE = (60, 200, 100)      # flecha (HAZ_ESTO)
COLOR_ROJO = (220, 40, 40)        # X (ERROR)


def actualizar_estado(job_id, **campos):
    with jobs_lock:
        jobs.setdefault(job_id, {}).update(campos)


def notificar_webhook(webhook_url, resultado):
    """Avisa a Make (u otro consumidor) que el job terminó, mandando el
    resultado final por POST. Si falla, no rompe el procesamiento: solo
    se loguea, porque el /estado/<job_id> sigue disponible como respaldo."""
    if not webhook_url:
        return
    try:
        requests.post(webhook_url, json=resultado, timeout=10)
    except Exception as e:
        print(f"Error notificando webhook: {e}")


def generar_overlay_miniatura(ruta_imagen, texto_miniatura, estrategia_miniatura, color_acento, ruta_fuente, ruta_salida):
    """Dibuja la miniatura final: una franja sólida en el tercio de la imagen
    que el prompt de Leonardo ya dejó vacío para texto (izquierda para
    ESTADO_PROBLEMA, arriba para las otras 3 estrategias), el texto centrado
    dentro de esa franja, y una flecha (HAZ_ESTO) o una X (ERROR) marcando
    el punto de interés sobre la imagen, según la estrategia elegida por
    Claude para este video."""
    imagen = Image.open(ruta_imagen).convert("RGB")
    ancho_img, alto_img = imagen.size

    if estrategia_miniatura == "ESTADO_PROBLEMA":
        franja_rect = (0, 0, int(ancho_img * 0.35), alto_img)
    else:
        franja_rect = (0, 0, ancho_img, int(alto_img * 0.30))

    overlay = Image.new("RGBA", imagen.size, (0, 0, 0, 0))
    dibujo_overlay = ImageDraw.Draw(overlay)
    dibujo_overlay.rectangle(franja_rect, fill=COLOR_FRANJA + (235,))
    imagen = Image.alpha_composite(imagen.convert("RGBA"), overlay).convert("RGB")
    dibujo = ImageDraw.Draw(imagen)

    # Texto: tamaño dinámico (arranca en 7% del ancho, igual que antes) hasta
    # que entre en el ancho disponible de la franja.
    titulo_impacto = texto_miniatura.upper()
    tamano_fuente = int(ancho_img * 0.07)
    fuente = ImageFont.truetype(ruta_fuente, tamano_fuente)
    ancho_franja = franja_rect[2] - franja_rect[0]
    lineas = textwrap.wrap(titulo_impacto, width=14)

    while True:
        anchos = [dibujo.textbbox((0, 0), linea, font=fuente)[2] for linea in lineas]
        if max(anchos, default=0) <= ancho_franja - 60 or tamano_fuente <= 40:
            break
        tamano_fuente -= 5
        fuente = ImageFont.truetype(ruta_fuente, tamano_fuente)

    alto_linea = tamano_fuente * 1.15
    alto_total_texto = alto_linea * len(lineas)
    centro_y = (franja_rect[1] + franja_rect[3]) / 2 - alto_total_texto / 2
    centro_x = (franja_rect[0] + franja_rect[2]) / 2

    for i, linea in enumerate(lineas):
        ancho_linea = dibujo.textbbox((0, 0), linea, font=fuente)[2]
        pos_x = centro_x - ancho_linea / 2
        pos_y = centro_y + (i * alto_linea)
        dibujo.text((pos_x, pos_y), linea, font=fuente, fill=COLOR_TEXTO)

    # Flecha o X, fuera de la franja (sobre la zona de la imagen)
    color_marca = COLOR_VERDE if color_acento == "verde" else COLOR_ROJO
    if estrategia_miniatura == "HAZ_ESTO":
        punto_inicio = (int(ancho_img * 0.40), int(alto_img * 0.45))
        punto_fin = (int(ancho_img * 0.60), int(alto_img * 0.68))
        dibujo.line([punto_inicio, punto_fin], fill=color_marca, width=12)
        dibujo.polygon([
            punto_fin,
            (punto_fin[0] - 25, punto_fin[1] - 10),
            (punto_fin[0] - 10, punto_fin[1] - 25),
        ], fill=color_marca)
    elif estrategia_miniatura == "ERROR":
        x0, y0 = int(ancho_img * 0.72), int(alto_img * 0.55)
        x1, y1 = int(ancho_img * 0.95), int(alto_img * 0.90)
        dibujo.line([(x0, y0), (x1, y1)], fill=color_marca, width=18)
        dibujo.line([(x0, y1), (x1, y0)], fill=color_marca, width=18)
    # ANTES_DESPUES no lleva marca extra: la franja + texto alcanza.

    imagen.save(ruta_salida, quality=95)


def construir_video_ffmpeg(rutas_imagenes, ruta_audio, ruta_salida, duracion_total,
                            fps=24, ancho=1920, alto=1080, duracion_transicion=1.0):
    """Arma el video multi-imagen con Ken Burns (filtro zoompan, nativo de ffmpeg,
    corre en C en vez de Python/PIL cuadro por cuadro) + crossfade entre escenas
    (filtro xfade). Reemplaza el approach anterior de moviepy+PIL, mucho más lento
    porque procesaba cada frame en Python puro."""
    n = len(rutas_imagenes)
    duracion_por_imagen = duracion_total / n
    # cada clip dura un poco más que su porción para poder solaparse en el xfade
    duracion_clip = duracion_por_imagen + duracion_transicion
    frames_por_clip = int(round(duracion_clip * fps))
    # escala previa al zoompan: 1.5x el ancho objetivo es justo lo necesario
    # para el zoom máximo de 1.5x sin pixelar — antes usaba 2x, que gastaba
    # memoria de más sin aportar nada (causó un "out of memory" en Render Starter)
    escala_previa = int(ancho * 1.5)

    cmd = ["ffmpeg", "-y"]
    for ruta in rutas_imagenes:
        cmd += ["-loop", "1", "-t", f"{duracion_clip:.3f}", "-i", ruta]
    cmd += ["-i", ruta_audio]

    filtros = []
    for i in range(n):
        zoom_in = (i % 2 == 0)
        if zoom_in:
            zoom_expr = "min(zoom+0.0015,1.5)"
            x_expr = f"iw/2-(iw/zoom/2)+(on/{frames_por_clip})*40"
        else:
            zoom_expr = "if(eq(on,0),1.5,max(zoom-0.0015,1.0))"
            x_expr = f"iw/2-(iw/zoom/2)-(on/{frames_por_clip})*40"
        y_expr = "ih/2-(ih/zoom/2)"
        filtros.append(
            f"[{i}:v]scale={escala_previa}:-1,zoompan=z='{zoom_expr}':x='{x_expr}':y='{y_expr}'"
            f":d={frames_por_clip}:s={ancho}x{alto}:fps={fps},setsar=1[v{i}]"
        )

    encadenado = "v0"
    offset_acumulado = duracion_clip - duracion_transicion
    for i in range(1, n):
        salida = f"vx{i}"
        filtros.append(
            f"[{encadenado}][v{i}]xfade=transition=fade:duration={duracion_transicion}:"
            f"offset={offset_acumulado:.3f}[{salida}]"
        )
        encadenado = salida
        offset_acumulado += duracion_clip - duracion_transicion

    cmd += [
        "-filter_complex", ";".join(filtros),
        "-map", f"[{encadenado}]",
        "-map", f"{n}:a",
        "-c:v", "libx264",
        "-preset", "ultrafast",  # menos memoria de buffer que veryfast, clave en un server de 512MB RAM
        "-threads", "2",
        "-crf", "23",
        "-c:a", "aac",
        "-shortest",
        ruta_salida,
    ]

    resultado = subprocess.run(cmd, capture_output=True, text=True)
    if resultado.returncode != 0:
        raise RuntimeError(f"ffmpeg falló al construir el video: {resultado.stderr[-2000:]}")


def generar_short(ruta_video_sin_subs, ruta_short, duracion_short=45):
    """Recorta los primeros N segundos del video (SIN subtítulos quemados)
    y lo recompone en vertical 9:16, recortando desde el centro del cuadro
    horizontal. Los subtítulos del Short se queman DESPUÉS de este recorte,
    no antes — si se queman antes (sobre el horizontal completo) y después
    se recorta a una tira vertical angosta, el texto centrado para el ancho
    completo queda literalmente cortado en los bordes al recortar.
    Usa ffmpeg directo (crop + trim) en vez de moviepy, mismo motivo: velocidad."""
    filtro_crop = "crop=ih*9/16:ih:(iw-ih*9/16)/2:0"
    cmd = [
        "ffmpeg", "-y",
        "-i", ruta_video_sin_subs,
        "-t", str(duracion_short),
        "-vf", filtro_crop,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-c:a", "aac",
        ruta_short,
    ]
    resultado = subprocess.run(cmd, capture_output=True, text=True)
    if resultado.returncode != 0:
        raise RuntimeError(f"ffmpeg falló al generar el short: {resultado.stderr[-2000:]}")


def _formato_srt(segundos):
    """Convierte segundos (float) al formato de timestamp que usa .srt:
    HH:MM:SS,mmm"""
    horas = int(segundos // 3600)
    minutos = int((segundos % 3600) // 60)
    segs = int(segundos % 60)
    milisegundos = int(round((segundos - int(segundos)) * 1000))
    return f"{horas:02d}:{minutos:02d}:{segs:02d},{milisegundos:03d}"


def _agrupar_en_bloques(palabras, palabras_por_bloque):
    """Agrupa una lista de palabras con timestamp (formato ElevenLabs Scribe)
    en bloques de N palabras, con corrección de solapamientos (si el bloque
    N termina después de que arranca el N+1, recorta el final del bloque N
    — sin esto, libass muestra dos subtítulos apilados al mismo tiempo).
    Reutilizado tanto para el video largo (bloques de 8) como para el Short
    (bloques de 4, más cortos, más cómodos en un cuadro angosto)."""
    bloques = []
    for i in range(0, len(palabras), palabras_por_bloque):
        grupo = palabras[i:i + palabras_por_bloque]
        if not grupo:
            continue
        texto = " ".join(w["text"] for w in grupo)
        bloques.append([grupo[0]["start"], grupo[-1]["end"], texto])

    for i in range(len(bloques) - 1):
        if bloques[i][1] > bloques[i + 1][0]:
            bloques[i][1] = bloques[i + 1][0]

    return bloques


def _escribir_srt(bloques, ruta_srt):
    with open(ruta_srt, "w", encoding="utf-8") as f:
        for idx, (inicio, fin, texto) in enumerate(bloques, start=1):
            f.write(f"{idx}\n")
            f.write(f"{_formato_srt(inicio)} --> {_formato_srt(fin)}\n")
            f.write(f"{texto}\n\n")


def generar_subtitulos(ruta_audio, ruta_srt, palabras_por_bloque=8):
    """Transcribe el audio con ElevenLabs Scribe (mismo proveedor/cuenta que
    ya usás para la narración, evita pelear con tarjetas rechazadas en otro
    proveedor). A diferencia de Whisper de OpenAI, esta API no devuelve el
    .srt directo — da timestamps palabra por palabra, así que los agrupamos
    en bloques de N palabras y armamos el .srt nosotros.

    Devuelve (bloques, palabras) — bloques para armar el .srt del video largo
    y para encontrar_corte_natural(); palabras (timestamps crudos) para poder
    armar un .srt distinto (bloques más cortos) para el Short, sin tener que
    llamar a la API de nuevo."""
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    with open(ruta_audio, "rb") as f:
        respuesta = requests.post(
            "https://api.elevenlabs.io/v1/speech-to-text",
            headers={"xi-api-key": api_key},
            data={"model_id": "scribe_v2", "timestamps_granularity": "word"},
            files={"file": f},
            timeout=300,
        )
    respuesta.raise_for_status()
    palabras = [w for w in respuesta.json().get("words", []) if w.get("type") == "word"]

    bloques = _agrupar_en_bloques(palabras, palabras_por_bloque)
    _escribir_srt(bloques, ruta_srt)

    return bloques, palabras


def encontrar_corte_natural(bloques, duracion_objetivo, tolerancia=5.0):
    """Busca el mejor segundo para cortar el Short cerca de duracion_objetivo,
    usando los mismos bloques de timestamps que ya arma generar_subtitulos()
    para los subtítulos — así el corte cae siempre al final de un bloque de
    palabras real, nunca a mitad de una palabra o frase.

    Preferencia: el último bloque, dentro de la ventana
    [duracion_objetivo - tolerancia, duracion_objetivo + tolerancia], cuyo
    texto termine en puntuación de cierre de frase (. ! ?) — para que el
    Short cierre una idea, no la corte. Si no hay ninguno así en la ventana,
    cae al último bloque que termine antes de duracion_objetivo + tolerancia
    (evita cortar a mitad de palabra, aunque no cierre una frase completa).
    """
    if not bloques:
        return duracion_objetivo

    limite_superior = duracion_objetivo + tolerancia
    limite_inferior = max(0.0, duracion_objetivo - tolerancia)

    candidatos_en_ventana = [b for b in bloques if limite_inferior <= b[1] <= limite_superior]

    for bloque in reversed(candidatos_en_ventana):
        texto = bloque[2].strip()
        if texto.endswith((".", "!", "?")):
            return bloque[1]

    if candidatos_en_ventana:
        return candidatos_en_ventana[-1][1]

    # Ningún bloque cayó en la ventana de tolerancia (guion muy corto, etc.)
    # — último recurso: el último bloque que termine antes del límite superior.
    anteriores = [b for b in bloques if b[1] <= limite_superior]
    if anteriores:
        return anteriores[-1][1]

    return duracion_objetivo


ESTILO_SUBTITULOS_LARGO = (
    "FontName=Arial,FontSize=22,PrimaryColour=&H00FFFFFF,"
    "OutlineColour=&H00000000,BorderStyle=3,Outline=2,Alignment=2,MarginV=60"
)

# Fuente más grande para el Short: el cuadro es angosto (9:16), así que hay
# menos ancho disponible por línea — compensamos con bloques de menos
# palabras (ver palabras_por_bloque=4 más abajo) y una fuente algo mayor,
# más cómoda de leer en pantalla de celular.
ESTILO_SUBTITULOS_SHORT = (
    "FontName=Arial,FontSize=26,PrimaryColour=&H00FFFFFF,"
    "OutlineColour=&H00000000,BorderStyle=3,Outline=2,Alignment=2,MarginV=80"
)


def quemar_subtitulos(ruta_video_entrada, ruta_srt, ruta_video_salida, estilo=None):
    """Quema los subtítulos sobre el video ya renderizado (filtro subtitles,
    vía libass). Estilo por defecto: blanco con borde negro, centrado abajo
    — pensado para el ancho del video horizontal. Para el Short, pasar
    estilo=ESTILO_SUBTITULOS_SHORT (fuente más grande, bloques más cortos)."""
    estilo = estilo or ESTILO_SUBTITULOS_LARGO
    filtro = f"subtitles={ruta_srt}:force_style='{estilo}'"
    cmd = [
        "ffmpeg", "-y",
        "-i", ruta_video_entrada,
        "-vf", filtro,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-c:a", "copy",
        ruta_video_salida,
    ]
    resultado = subprocess.run(cmd, capture_output=True, text=True)
    if resultado.returncode != 0:
        raise RuntimeError(f"ffmpeg falló al quemar subtítulos: {resultado.stderr[-2000:]}")


def procesar_activo(job_id, titulo, imagenes_urls, rutas_audio_partes, duracion_short, webhook_url=None,
                     descripcion_seo="", hashtags="", etiquetas_ocultas="", fila="", texto_miniatura="",
                     imagen_miniatura_url="", estrategia_miniatura="", color_acento="verde"):
    """Trabajo pesado que corre en un hilo de fondo. Usa rutas con el job_id
    para que jobs concurrentes no se pisen los archivos intermedios."""
    rutas_imagenes = [f"imagen_{job_id}_{i}.jpg" for i in range(len(imagenes_urls))]
    ruta_miniatura = f"miniatura_final_{job_id}.jpg"
    ruta_imagen_miniatura_fuente = f"miniatura_fuente_{job_id}.jpg"
    ruta_video_sin_subs = f"video_sin_subs_{job_id}.mp4"
    ruta_video = f"video_final_{job_id}.mp4"
    ruta_short = f"short_{job_id}.mp4"
    ruta_short_sin_subs = f"short_sin_subs_{job_id}.mp4"
    ruta_srt_short = f"subtitulos_short_{job_id}.srt"
    ruta_fuente = "Anton-Regular.ttf"
    ruta_audio = f"audio_unido_{job_id}.mp3"
    ruta_srt = f"subtitulos_{job_id}.srt"

    semaforo_render.acquire()
    actualizar_estado(job_id, status="procesando")  # confirma que ya salió de la cola y arrancó de verdad
    try:
        # 0. Unir las partes de audio (ElevenLabs las manda separadas por el
        # límite de 10.000 caracteres por request; acá se juntan en un solo mp3)
        audio_unido = AudioSegment.empty()
        for parte in rutas_audio_partes:
            audio_unido += AudioSegment.from_file(parte)
        audio_unido.export(ruta_audio, format="mp3")

        # 1. Descargar todas las imágenes (Leonardo AI)
        for url, ruta in zip(imagenes_urls, rutas_imagenes):
            respuesta_img = requests.get(url)
            with open(ruta, 'wb') as f:
                f.write(respuesta_img.content)

        # 2. Fabricar la Miniatura: usa la imagen DEDICADA de Leonardo
        # (generada con prompt_miniatura) si llegó su URL; si no, cae al
        # comportamiento anterior (reusar la primera imagen de escena) para
        # no romper corridas viejas o filas que todavía no manden ese campo.
        if imagen_miniatura_url:
            respuesta_mini = requests.get(imagen_miniatura_url)
            with open(ruta_imagen_miniatura_fuente, 'wb') as f:
                f.write(respuesta_mini.content)
        else:
            ruta_imagen_miniatura_fuente = rutas_imagenes[0]

        if os.path.exists(ruta_fuente) and os.path.exists(ruta_imagen_miniatura_fuente):
            # Usa el gancho corto (texto_miniatura, máx. 4 palabras) en vez
            # del título largo de SEO — son campos distintos que Claude
            # genera por separado. Si por algún motivo no llega, usa el
            # título como respaldo en vez de dejar la miniatura sin texto.
            titulo_impacto = texto_miniatura or titulo
            generar_overlay_miniatura(
                ruta_imagen_miniatura_fuente, titulo_impacto,
                estrategia_miniatura or "ESTADO_PROBLEMA", color_acento or "verde",
                ruta_fuente, ruta_miniatura,
            )

        # 3. Fabricar el Video: multi-imagen + Ken Burns (zoompan) + crossfade (xfade),
        # todo vía ffmpeg directo — reemplaza el pipeline anterior de moviepy/PIL.
        duracion_total = len(audio_unido) / 1000.0  # pydub mide en milisegundos
        construir_video_ffmpeg(rutas_imagenes, ruta_audio, ruta_video_sin_subs, duracion_total)

        # 3.5. Transcribir el audio con ElevenLabs Scribe para generar los subtítulos
        bloques_subtitulos, palabras_subtitulos = generar_subtitulos(ruta_audio, ruta_srt)

        # 3.6. Quemar los subtítulos sobre el video largo ya renderizado
        quemar_subtitulos(ruta_video_sin_subs, ruta_srt, ruta_video)

        # 4. Generar el Short: corte "inteligente" a partir del video SIN
        # subtítulos (video_sin_subs), no del video largo ya subtitulado —
        # así el recorte a vertical pasa primero, y los subtítulos se queman
        # DESPUÉS sobre el cuadro angosto ya recortado. En el orden anterior
        # (subtitular sobre el horizontal completo y recortar después), el
        # texto centrado para el ancho completo quedaba cortado en los bordes
        # al angostar el cuadro a 9:16.
        corte_short = encontrar_corte_natural(bloques_subtitulos, duracion_short)
        generar_short(ruta_video_sin_subs, ruta_short_sin_subs, duracion_short=corte_short)

        # 4.5. Armar un .srt propio para el Short, con bloques de 4 palabras
        # (en vez de 8) — se leen mejor en un cuadro angosto. Reusa los
        # timestamps ya obtenidos de ElevenLabs Scribe, filtrados a la
        # duración real del corte — no hace falta llamar la API de nuevo.
        palabras_del_short = [w for w in palabras_subtitulos if w["start"] < corte_short]
        bloques_short = _agrupar_en_bloques(palabras_del_short, palabras_por_bloque=4)
        _escribir_srt(bloques_short, ruta_srt_short)
        quemar_subtitulos(ruta_short_sin_subs, ruta_srt_short, ruta_short, estilo=ESTILO_SUBTITULOS_SHORT)

        # 5. Subida Automática a Cloudinary (video largo + miniatura + short)
        url_miniatura_publica = ""
        url_video_publica = ""
        url_short_publica = ""

        if os.path.exists(ruta_miniatura):
            upload_img = cloudinary.uploader.upload(ruta_miniatura, resource_type="image")
            url_miniatura_publica = upload_img.get("secure_url", "")

        if os.path.exists(ruta_video):
            upload_vid = cloudinary.uploader.upload(ruta_video, resource_type="video")
            url_video_publica = upload_vid.get("secure_url", "")

        if os.path.exists(ruta_short):
            upload_short = cloudinary.uploader.upload(ruta_short, resource_type="video")
            url_short_publica = upload_short.get("secure_url", "")

        resultado = {
            "job_id": job_id,
            "status": "listo",
            "url_video": url_video_publica,
            "url_miniatura": url_miniatura_publica,
            "url_short": url_short_publica,
            "titulo_seo": titulo,
            "descripcion_seo": descripcion_seo,
            "hashtags": hashtags,
            "etiquetas_ocultas": etiquetas_ocultas,
            "fila": fila,
        }
        actualizar_estado(**resultado)
        notificar_webhook(webhook_url, resultado)

    except Exception as e:
        resultado_error = {"job_id": job_id, "status": "error", "mensaje": str(e), "fila": fila}
        actualizar_estado(**resultado_error)
        notificar_webhook(webhook_url, resultado_error)

    finally:
        semaforo_render.release()
        # Limpiar los archivos intermedios de este job
        rutas_a_borrar = (
            [ruta_audio, ruta_miniatura, ruta_imagen_miniatura_fuente, ruta_video_sin_subs,
             ruta_video, ruta_short, ruta_short_sin_subs, ruta_srt, ruta_srt_short]
            + rutas_imagenes + rutas_audio_partes
        )
        for ruta in rutas_a_borrar:
            try:
                if os.path.exists(ruta):
                    os.remove(ruta)
            except OSError:
                pass


def _lanzar_render(titulo, lista_urls, rutas_audio_partes, duracion_short, webhook_url,
                    descripcion_seo, hashtags, etiquetas_ocultas, fila, texto_miniatura="",
                    imagen_miniatura_url="", estrategia_miniatura="", color_acento="verde"):
    """Registra el job y dispara procesar_activo en un hilo de fondo.
    Compartido por /fabricar (llamada directa, para pruebas manuales) y por
    /acumular_imagen (cuando ya juntó las N imágenes del Iterator)."""
    job_id = uuid.uuid4().hex
    actualizar_estado(job_id, status="procesando")
    hilo = threading.Thread(
        target=procesar_activo,
        args=(job_id, titulo, lista_urls, rutas_audio_partes, duracion_short, webhook_url,
              descripcion_seo, hashtags, etiquetas_ocultas, fila, texto_miniatura,
              imagen_miniatura_url, estrategia_miniatura, color_acento),
        daemon=True,
    )
    hilo.start()
    return job_id


@app.route('/acumular_imagen', methods=['POST'])
def acumular_imagen():
    """Endpoint que Make llama UNA VEZ POR IMAGEN, adentro del loop del
    Iterator (reemplaza al Aggregator de Make, que tenía un bug de
    plataforma confirmado: no cerraba el ciclo del Iterator de forma
    confiable — mismo síntoma reportado por otros usuarios en el foro
    oficial de Make, sin solución en varios intentos).

    Cada llamada manda: job_key (usamos el número de fila del Sheet, estable
    por contenido), url (la imagen de ESTA iteración), posicion y total
    (Bundle order position / Total number of bundles, que el Iterator ya
    expone gratis). El resto de los campos (titulo, audio, webhook_url, etc.)
    se mandan en TODAS las llamadas (son iguales en las 4, Make no permite
    mandarlos solo en la última sin lógica extra) — Flask simplemente los
    vuelve a guardar cada vez, sin problema porque el contenido es idéntico.

    Cuando llega la última imagen (posicion == total), recién ahí se dispara
    el render completo con las URLs ya ordenadas."""
    job_key = request.form.get('job_key', '').strip()
    url_imagen = request.form.get('url', '').strip()
    try:
        posicion = int(request.form.get('posicion', 0))
        total = int(request.form.get('total', 0))
    except ValueError:
        return jsonify({"status": "error", "mensaje": "posicion/total inválidos"}), 400

    if not job_key or not url_imagen or not posicion or not total:
        return jsonify({"status": "error", "mensaje": "Faltan job_key, url, posicion o total"}), 400

    titulo = request.form.get('titulo', 'EL SECRETO ESTOICO')
    texto_miniatura = request.form.get('texto_miniatura', '')
    imagen_miniatura_url = request.form.get('imagen_miniatura_url', '')
    estrategia_miniatura = request.form.get('estrategia_miniatura', '')
    color_acento = request.form.get('color_acento', 'verde')
    webhook_url = request.form.get('webhook_url')
    descripcion_seo = request.form.get('descripcion_seo', '')
    hashtags = request.form.get('hashtags', '')
    etiquetas_ocultas = request.form.get('etiquetas_ocultas', '')
    fila = request.form.get('fila', '')
    try:
        duracion_short = int(request.form.get('duracion_short', 45))
    except ValueError:
        duracion_short = 45

    with acumuladores_lock:
        entrada = acumuladores.setdefault(job_key, {"urls": {}, "meta": {}})
        entrada["urls"][posicion] = url_imagen
        entrada["meta"] = {
            "titulo": titulo,
            "texto_miniatura": texto_miniatura,
            "imagen_miniatura_url": imagen_miniatura_url,
            "estrategia_miniatura": estrategia_miniatura,
            "color_acento": color_acento,
            "webhook_url": webhook_url,
            "descripcion_seo": descripcion_seo,
            "hashtags": hashtags,
            "etiquetas_ocultas": etiquetas_ocultas,
            "fila": fila,
            "duracion_short": duracion_short,
        }
        completo = len(entrada["urls"]) >= total

    # Las partes de audio se guardan siempre con nombre fijo por job_key
    # (se pisan en cada llamada con el mismo contenido, no hay problema:
    # solo se usan de verdad recién cuando se dispara el render).
    rutas_audio_partes = []
    i = 1
    while f'audio_parte_{i}' in request.files:
        ruta_parte = f"audio_parte_{i}_{job_key}.mp3"
        request.files[f'audio_parte_{i}'].save(ruta_parte)
        rutas_audio_partes.append(ruta_parte)
        i += 1

    if not completo:
        # Todavía faltan imágenes por llegar — solo confirmamos recepción.
        return jsonify({"status": "acumulando", "recibidas": len(entrada["urls"]), "total": total}), 202

    # Llegó la última — armamos la lista ordenada y disparamos el render.
    with acumuladores_lock:
        entrada = acumuladores.pop(job_key, entrada)
    lista_urls = [entrada["urls"][pos] for pos in sorted(entrada["urls"])]
    meta = entrada["meta"]

    if not rutas_audio_partes:
        return jsonify({"status": "error", "mensaje": "Faltan las partes de audio (audio_parte_1, audio_parte_2, ...)"}), 400

    job_id = _lanzar_render(
        meta["titulo"], lista_urls, rutas_audio_partes, meta["duracion_short"], meta["webhook_url"],
        meta["descripcion_seo"], meta["hashtags"], meta["etiquetas_ocultas"], meta["fila"],
        meta.get("texto_miniatura", ""), meta.get("imagen_miniatura_url", ""),
        meta.get("estrategia_miniatura", ""), meta.get("color_acento", "verde"),
    )
    return jsonify({"job_id": job_id, "status": "render_iniciado"}), 202


@app.route('/fabricar', methods=['POST'])
def fabricar_activo():
    # 1. Desempaquetar los textos
    titulo = request.form.get('titulo', 'EL SECRETO ESTOICO')
    texto_miniatura = request.form.get('texto_miniatura', '')
    imagen_miniatura_url = request.form.get('imagen_miniatura_url', '')
    estrategia_miniatura = request.form.get('estrategia_miniatura', '')
    color_acento = request.form.get('color_acento', 'verde')
    imagenes_urls_raw = request.form.get('imagenes_urls', '')
    lista_urls = [u.strip() for u in imagenes_urls_raw.split(',') if u.strip()]
    webhook_url = request.form.get('webhook_url')  # opcional: URL de callback de Make
    # Metadata SEO que solo se necesita más adelante (YouTube Upload), en el
    # escenario del webhook — viaja "de pasada" para no perderla al cortar acá.
    descripcion_seo = request.form.get('descripcion_seo', '')
    hashtags = request.form.get('hashtags', '')
    etiquetas_ocultas = request.form.get('etiquetas_ocultas', '')
    fila = request.form.get('fila', '')  # número de fila del Sheet, para poder actualizarla desde el escenario del webhook

    try:
        duracion_short = int(request.form.get('duracion_short', 45))
    except ValueError:
        duracion_short = 45

    # 2. Validar entradas
    if not lista_urls:
        return jsonify({"status": "error", "mensaje": "Falta imagenes_urls"}), 400

    # 3. Guardar todas las partes de audio recibidas (audio_parte_1, audio_parte_2, ...)
    job_id_temp = uuid.uuid4().hex
    rutas_audio_partes = []
    i = 1
    while f'audio_parte_{i}' in request.files:
        ruta_parte = f"audio_parte_{i}_{job_id_temp}.mp3"
        request.files[f'audio_parte_{i}'].save(ruta_parte)
        rutas_audio_partes.append(ruta_parte)
        i += 1

    if not rutas_audio_partes:
        return jsonify({"status": "error", "mensaje": "Faltan las partes de audio (audio_parte_1, audio_parte_2, ...)"}), 400

    # 4. Registrar el job y disparar el procesamiento en segundo plano
    job_id = _lanzar_render(titulo, lista_urls, rutas_audio_partes, duracion_short, webhook_url,
                             descripcion_seo, hashtags, etiquetas_ocultas, fila, texto_miniatura,
                             imagen_miniatura_url, estrategia_miniatura, color_acento)

    # 5. Responder de inmediato
    return jsonify({"job_id": job_id}), 202


@app.route('/estado/<job_id>', methods=['GET'])
def estado_job(job_id):
    with jobs_lock:
        estado = jobs.get(job_id)
        estado = dict(estado) if estado else None

    if estado is None:
        return jsonify({"status": "error", "mensaje": "job_id no encontrado"}), 404

    return jsonify(estado)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
