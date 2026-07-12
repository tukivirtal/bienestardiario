import os
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

@app.route('/fabricar', methods=['POST'])
def fabricar_activo():
    try:
        # 1. Desempaquetar los textos
        titulo = request.form.get('titulo', 'EL SECRETO ESTOICO')
        imagen_url = request.form.get('imagen_url')
        
        # 2. Desempaquetar y guardar el archivo de audio
        if 'audio' not in request.files:
            return jsonify({"status": "error", "mensaje": "Falta el archivo de audio"}), 400
        
        archivo_audio = request.files['audio']
        ruta_audio = "audio_recibido.mp3"
        archivo_audio.save(ruta_audio)

        # 3. Descargar la imagen de Leonardo AI
        ruta_imagen = "imagen_base.jpg"
        if imagen_url:
            respuesta_img = requests.get(imagen_url)
            with open(ruta_imagen, 'wb') as f:
                f.write(respuesta_img.content)
        else:
            return jsonify({"status": "error", "mensaje": "Falta la URL de la imagen"}), 400

      # 4. Fabricar la Miniatura (Diseño Profesional Proporcional)
        ruta_fuente = "Anton-Regular.ttf"
        ruta_miniatura = "miniatura_final.jpg"
        
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

        # 5. Fabricar el Video (1 FPS para proteger la RAM)
        ruta_video = "video_final.mp4"
        audio_clip = AudioFileClip(ruta_audio)
        image_clip = ImageClip(ruta_imagen).set_duration(audio_clip.duration)
        
        video = image_clip.set_audio(audio_clip)
        video.write_videofile(ruta_video, fps=1, codec="libx264", audio_codec="aac")
        
        audio_clip.close()
        image_clip.close()

        # 6. Subida Automática a Cloudinary
        url_miniatura_publica = ""
        url_video_publica = ""
        
        if os.path.exists(ruta_miniatura):
            upload_img = cloudinary.uploader.upload(ruta_miniatura, resource_type="image")
            url_miniatura_publica = upload_img.get("secure_url", "")
            
        if os.path.exists(ruta_video):
            upload_vid = cloudinary.uploader.upload(ruta_video, resource_type="video")
            url_video_publica = upload_vid.get("secure_url", "")

        return jsonify({
            "status": "éxito",
            "url_video": url_video_publica,
            "url_miniatura": url_miniatura_publica
        })
        
    except Exception as e:
        return jsonify({"status": "error", "mensaje": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
