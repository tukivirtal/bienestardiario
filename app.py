import os
import math
import uuid
import threading
import requests
import textwrap
import numpy as np
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from PIL import Image, ImageDraw, ImageFont
from moviepy.editor import ImageClip, AudioFileClip, VideoFileClip, concatenate_videoclips
from moviepy.video.fx.all import crop
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


def efecto_ken_burns(clip, zoom_ratio=0.03):
    """Aplica un zoom lento y progresivo sobre el clip (efecto Ken Burns)."""
    def efecto(get_frame, t):
        img = Image.fromarray(get_frame(t))
        base_size = img.size
        new_size = [
            math.ceil(img.size[0] * (1 + (zoom_ratio * t))),
            math.ceil(img.size[1] * (1 + (zoom_ratio * t)))
        ]
        new_size[0] += new_size[0] % 2
        new_size[1] += new_size[1] % 2
        img = img.resize(new_size, Image.LANCZOS)
        x = math.ceil((new_size[0] - base_size[0]) / 2)
        y = math.ceil((new_size[1] - base_size[1]) / 2)
        img = img.crop((x, y, new_size[0] - x, new_size[1] - y)).resize(base_size, Image.LANCZOS)
        resultado = np.array(img)
        img.close()
        return resultado
    return clip.fl(efecto)


def generar_short(ruta_video_largo, ruta_short, duracion_short=45):
    """Recorta los primeros N segundos del video largo y lo recompone en
    vertical 9:16, recortando desde el centro del cuadro horizontal."""
    clip_largo = VideoFileClip(ruta_video_largo)
    duracion = min(duracion_short, clip_largo.duration)
    fragmento = clip_largo.subclip(0, duracion)

    ancho, alto = fragmento.size
    ancho_objetivo = int(alto * 9 / 16)

    if ancho_objetivo < ancho:
        fragmento_vertical = crop(
            fragmento,
            width=ancho_objetivo,
            height=alto,
            x_center=ancho / 2,
            y_center=alto / 2,
        )
    else:
        # el video ya es más angosto que 9:16, lo dejamos como está
        fragmento_vertical = fragmento

    fragmento_vertical.write_videofile(ruta_short, fps=24, codec="libx264", audio_codec="aac")

    fragmento_vertical.close()
    fragmento.close()
    clip_largo.close()


def procesar_activo(job_id, titulo, imagenes_urls, rutas_audio_partes, duracion_short, webhook_url=None):
    """Trabajo pesado que corre en un hilo de fondo. Usa rutas con el job_id
    para que jobs concurrentes no se pisen los archivos intermedios."""
    rutas_imagenes = [f"imagen_{job_id}_{i}.jpg" for i in range(len(imagenes_urls))]
    ruta_miniatura = f"miniatura_final_{job_id}.jpg"
    ruta_video = f"video_final_{job_id}.mp4"
    ruta_short = f"short_{job_id}.mp4"
    ruta_fuente = "Anton-Regular.ttf"
    ruta_audio = f"audio_unido_{job_id}.mp3"

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

        # 3. Fabricar el Video: multi-imagen + Ken Burns + crossfade entre escenas
        audio_clip = AudioFileClip(ruta_audio)
        duracion_total = audio_clip.duration
        duracion_por_imagen = duracion_total / len(rutas_imagenes)

        clips = []
        for ruta in rutas_imagenes:
            clip = ImageClip(ruta).set_duration(duracion_por_imagen + 1)
            clip = efecto_ken_burns(clip, zoom_ratio=0.03)
            clip = clip.crossfadein(1)
            clips.append(clip)

        video = concatenate_videoclips(clips, method="compose", padding=-1)
        video = video.set_duration(duracion_total).set_audio(audio_clip)
        video.write_videofile(ruta_video, fps=24, codec="libx264", audio_codec="aac")

        audio_clip.close()
        for clip in clips:
            clip.close()
        video.close()

        # 4. Generar el Short a partir del video largo ya renderizado
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
        }
        actualizar_estado(job_id, **resultado)
        notificar_webhook(webhook_url, resultado)

    except Exception as e:
        resultado_error = {"job_id": job_id, "status": "error", "mensaje": str(e)}
        actualizar_estado(job_id, **resultado_error)
        notificar_webhook(webhook_url, resultado_error)

    finally:
        # Limpiar los archivos intermedios de este job
        rutas_a_borrar = [ruta_audio, ruta_miniatura, ruta_video, ruta_short] + rutas_imagenes + rutas_audio_partes
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
        args=(job_id, titulo, lista_urls, rutas_audio_partes, duracion_short, webhook_url),
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
