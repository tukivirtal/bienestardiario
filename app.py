import os
import uuid
import threading
import requests
import textwrap
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from PIL import Image, ImageDraw, ImageFont
from moviepy.editor import ImageClip, AudioFileClip
import cloudinary
import cloudinary.uploader

load_dotenv()

app = Flask(__name__)

# 🛠️ CONFIGURACIÓN DE CLOUDINARY: valores tomados de variables de entorno (ver .env.example)
cloudinary.config(
  cloud_name = os.environ.get("CLOUDINARY_CLOUD_NAME"),
  api_key = os.environ.get("CLOUDINARY_API_KEY"),
  api_secret = os.environ.get("CLOUDINARY_API_SECRET"),
  secure = True
)

# Estado de los jobs en memoria (por ahora). Clave: job_id -> dict de estado.
# Protegido con un lock porque lo escribe el hilo de fondo y lo lee el request.
jobs = {}
jobs_lock = threading.Lock()


def actualizar_estado(job_id, **campos):
    with jobs_lock:
        jobs.setdefault(job_id, {}).update(campos)


def procesar_activo(job_id, titulo, imagen_url, ruta_audio):
    """Trabajo pesado que corre en un hilo de fondo. Usa rutas con el job_id
    para que jobs concurrentes no se pisen los archivos intermedios."""
    ruta_imagen = f"imagen_base_{job_id}.jpg"
    ruta_miniatura = f"miniatura_final_{job_id}.jpg"
    ruta_video = f"video_final_{job_id}.mp4"
    ruta_fuente = "Anton-Regular.ttf"

    try:
        # 1. Descargar la imagen de Leonardo AI
        respuesta_img = requests.get(imagen_url)
        with open(ruta_imagen, 'wb') as f:
            f.write(respuesta_img.content)

        # 2. Fabricar la Miniatura (Diseño Profesional Proporcional)
        if os.path.exists(ruta_fuente) and os.path.exists(ruta_imagen):
            imagen = Image.open(ruta_imagen)
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

        # 3. Fabricar el Video (1 FPS para proteger la RAM)
        audio_clip = AudioFileClip(ruta_audio)
        image_clip = ImageClip(ruta_imagen).set_duration(audio_clip.duration)

        video = image_clip.set_audio(audio_clip)
        video.write_videofile(ruta_video, fps=1, codec="libx264", audio_codec="aac")

        audio_clip.close()
        image_clip.close()

        # 4. Subida Automática a Cloudinary
        url_miniatura_publica = ""
        url_video_publica = ""

        if os.path.exists(ruta_miniatura):
            upload_img = cloudinary.uploader.upload(ruta_miniatura, resource_type="image")
            url_miniatura_publica = upload_img.get("secure_url", "")

        if os.path.exists(ruta_video):
            upload_vid = cloudinary.uploader.upload(ruta_video, resource_type="video")
            url_video_publica = upload_vid.get("secure_url", "")

        actualizar_estado(
            job_id,
            status="listo",
            url_video=url_video_publica,
            url_miniatura=url_miniatura_publica,
        )

    except Exception as e:
        actualizar_estado(job_id, status="error", mensaje=str(e))

    finally:
        # Limpiar los archivos intermedios de este job
        for ruta in (ruta_audio, ruta_imagen, ruta_miniatura, ruta_video):
            try:
                if os.path.exists(ruta):
                    os.remove(ruta)
            except OSError:
                pass


@app.route('/fabricar', methods=['POST'])
def fabricar_activo():
    # 1. Desempaquetar los textos
    titulo = request.form.get('titulo', 'EL SECRETO ESTOICO')
    imagen_url = request.form.get('imagen_url')

    # 2. Validar entradas
    if 'audio' not in request.files:
        return jsonify({"status": "error", "mensaje": "Falta el archivo de audio"}), 400
    if not imagen_url:
        return jsonify({"status": "error", "mensaje": "Falta la URL de la imagen"}), 400

    # 3. Guardar el audio AHORA (el FileStorage del request no sobrevive al hilo)
    job_id = uuid.uuid4().hex
    ruta_audio = f"audio_recibido_{job_id}.mp3"
    request.files['audio'].save(ruta_audio)

    # 4. Registrar el job y disparar el procesamiento en segundo plano
    actualizar_estado(job_id, status="procesando")
    hilo = threading.Thread(
        target=procesar_activo,
        args=(job_id, titulo, imagen_url, ruta_audio),
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
