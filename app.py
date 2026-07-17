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
    # escala previa al zoompan: 2x el ancho objetivo alcanza para zoom hasta 1.5x
    # sin pixelar, y es mucho más rápido que sobre-escalar a un tamaño fijo enorme
    escala_previa = ancho * 2

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
        "-preset", "veryfast",
        "-crf", "20",
        "-c:a", "aac",
        "-shortest",
        ruta_salida,
    ]

    resultado = subprocess.run(cmd, capture_output=True, text=True)
    if resultado.returncode != 0:
        raise RuntimeError(f"ffmpeg falló al construir el video: {resultado.stderr[-2000:]}")


def generar_short(ruta_video_largo, ruta_short, duracion_short=45):
    """Recorta los primeros N segundos del video largo y lo recompone en
    vertical 9:16, recortando desde el centro del cuadro horizontal.
    Usa ffmpeg directo (crop + trim) en vez de moviepy, mismo motivo: velocidad."""
    filtro_crop = "crop=ih*9/16:ih:(iw-ih*9/16)/2:0"
    cmd = [
        "ffmpeg", "-y",
        "-i", ruta_video_largo,
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


def generar_subtitulos(ruta_audio, ruta_srt, palabras_por_bloque=8):
    """Transcribe el audio con ElevenLabs Scribe (mismo proveedor/cuenta que
    ya usás para la narración, evita pelear con tarjetas rechazadas en otro
    proveedor). A diferencia de Whisper de OpenAI, esta API no devuelve el
    .srt directo — da timestamps palabra por palabra, así que los agrupamos
    en bloques de N palabras y armamos el .srt nosotros."""
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

    bloques = []
    for i in range(0, len(palabras), palabras_por_bloque):
        grupo = palabras[i:i + palabras_por_bloque]
        if not grupo:
            continue
        texto = " ".join(w["text"] for w in grupo)
        bloques.append((grupo[0]["start"], grupo[-1]["end"], texto))

    with open(ruta_srt, "w", encoding="utf-8") as f:
        for idx, (inicio, fin, texto) in enumerate(bloques, start=1):
            f.write(f"{idx}\n")
            f.write(f"{_formato_srt(inicio)} --> {_formato_srt(fin)}\n")
            f.write(f"{texto}\n\n")


def quemar_subtitulos(ruta_video_entrada, ruta_srt, ruta_video_salida):
    """Quema los subtítulos sobre el video ya renderizado (filtro subtitles,
    vía libass). Estilo: blanco con borde negro, centrado abajo."""
    estilo = (
        "FontName=Arial,FontSize=22,PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,BorderStyle=3,Outline=2,Alignment=2,MarginV=60"
    )
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
                     descripcion_seo="", hashtags="", etiquetas_ocultas="", fila=""):
    """Trabajo pesado que corre en un hilo de fondo. Usa rutas con el job_id
    para que jobs concurrentes no se pisen los archivos intermedios."""
    rutas_imagenes = [f"imagen_{job_id}_{i}.jpg" for i in range(len(imagenes_urls))]
    ruta_miniatura = f"miniatura_final_{job_id}.jpg"
    ruta_video_sin_subs = f"video_sin_subs_{job_id}.mp4"
    ruta_video = f"video_final_{job_id}.mp4"
    ruta_short = f"short_{job_id}.mp4"
    ruta_fuente = "Anton-Regular.ttf"
    ruta_audio = f"audio_unido_{job_id}.mp3"
    ruta_srt = f"subtitulos_{job_id}.srt"

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

        # 2. Fabricar la Miniatura (se arma con la PRIMERA imagen)
        ruta_imagen_portada = rutas_imagenes[0]
        if os.path.exists(ruta_fuente) and os.path.exists(ruta_imagen_portada):
            imagen = Image.open(ruta_imagen_portada)
            dibujo = ImageDraw.Draw(imagen)

            ancho_img, alto_img = imagen.size
            titulo_impacto = titulo.upper()
            tamano_fuente = int(ancho_img * 0.07)
            fuente = ImageFont.truetype(ruta_fuente, tamano_fuente)

            lineas = textwrap.wrap(titulo_impacto, width=14)

            pos_x = ancho_img * 0.05
            pos_y = alto_img * 0.15
            alto_linea = tamano_fuente * 1.15

            for i, linea in enumerate(lineas):
                y_actual = pos_y + (i * alto_linea)
                dibujo.text((pos_x + 4, y_actual + 4), linea, font=fuente, fill="black")
                dibujo.text((pos_x, y_actual), linea, font=fuente, fill="#ffde59")

            imagen.save(ruta_miniatura)

        # 3. Fabricar el Video: multi-imagen + Ken Burns (zoompan) + crossfade (xfade),
        # todo vía ffmpeg directo — reemplaza el pipeline anterior de moviepy/PIL.
        duracion_total = len(audio_unido) / 1000.0  # pydub mide en milisegundos
        construir_video_ffmpeg(rutas_imagenes, ruta_audio, ruta_video_sin_subs, duracion_total)

        # 3.5. Transcribir el audio con ElevenLabs Scribe para generar los subtítulos
        generar_subtitulos(ruta_audio, ruta_srt)

        # 3.6. Quemar los subtítulos sobre el video ya renderizado
        quemar_subtitulos(ruta_video_sin_subs, ruta_srt, ruta_video)

        # 4. Generar el Short a partir del video largo YA CON subtítulos quemados
        generar_short(ruta_video, ruta_short, duracion_short=duracion_short)

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
        actualizar_estado(job_id, **resultado)
        notificar_webhook(webhook_url, resultado)

    except Exception as e:
        resultado_error = {"job_id": job_id, "status": "error", "mensaje": str(e), "fila": fila}
        actualizar_estado(job_id, **resultado_error)
        notificar_webhook(webhook_url, resultado_error)

    finally:
        # Limpiar los archivos intermedios de este job
        rutas_a_borrar = [ruta_audio, ruta_miniatura, ruta_video_sin_subs, ruta_video, ruta_short, ruta_srt] + rutas_imagenes + rutas_audio_partes
        for ruta in rutas_a_borrar:
            try:
                if os.path.exists(ruta):
                    os.remove(ruta)
            except OSError:
                pass


@app.route('/fabricar', methods=['POST'])
def fabricar_activo():
    # 1. Desempaquetar los textos
    titulo = request.form.get('titulo', 'EL SECRETO ESTOICO')
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
    job_id = uuid.uuid4().hex
    rutas_audio_partes = []
    i = 1
    while f'audio_parte_{i}' in request.files:
        ruta_parte = f"audio_parte_{i}_{job_id}.mp3"
        request.files[f'audio_parte_{i}'].save(ruta_parte)
        rutas_audio_partes.append(ruta_parte)
        i += 1

    if not rutas_audio_partes:
        return jsonify({"status": "error", "mensaje": "Faltan las partes de audio (audio_parte_1, audio_parte_2, ...)"}), 400

    # 4. Registrar el job y disparar el procesamiento en segundo plano
    actualizar_estado(job_id, status="procesando")
    hilo = threading.Thread(
        target=procesar_activo,
        args=(job_id, titulo, lista_urls, rutas_audio_partes, duracion_short, webhook_url,
              descripcion_seo, hashtags, etiquetas_ocultas, fila),
        daemon=True,
    )
    hilo.start()

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
