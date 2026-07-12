# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Single-file Flask microservice (`app.py`) that "fabricates" social-media assets. It exposes one endpoint, `POST /fabricar`, which takes a title, an image URL, and an uploaded audio file, then:

1. Downloads the source image (typically from Leonardo AI) to `imagen_base.jpg`.
2. Renders a YouTube-style thumbnail (`miniatura_final.jpg`) by overlaying the uppercased, word-wrapped title in the Anton font (`Anton-Regular.ttf`) with a black drop-shadow and `#ffde59` fill.
3. Builds a video (`video_final.mp4`) from the still image + audio via moviepy, encoded at **1 FPS** (`libx264`/`aac`) — deliberately low to conserve RAM on constrained hosts.
4. Uploads both the thumbnail and video to Cloudinary and returns their `secure_url`s as JSON.

The service is designed to be called by an external automation pipeline (n8n / Make style), not a browser UI. All user-facing strings and comments are in Spanish.

## Commands

```bash
# Install deps
pip install -r requirements.txt   # also needs ffmpeg on the system (see Dockerfile)

# Run locally (serves on 0.0.0.0:10000)
python app.py

# Build & run the container
docker build -t opioracional .
docker run -p 10000:10000 opioracional

# Smoke-test the endpoint
curl -X POST http://localhost:10000/fabricar \
  -F "titulo=EL SECRETO ESTOICO" \
  -F "imagen_url=https://example.com/img.jpg" \
  -F "audio=@audio_recibido.mp3"
```

There is no test suite, linter, or build step beyond Docker.

## Deploy (Render)

The service runs on Render (repo `github.com/tukivirtal/opioracional`, branch `main`), built from the `Dockerfile`.

1. **Service type:** Web Service, Docker environment. Render builds the `Dockerfile` (which installs `ffmpeg` — required by moviepy). No build/start command overrides needed; the image's `CMD ["python", "app.py"]` serves the app.
2. **Port:** the app binds `0.0.0.0:10000`. Render auto-detects the exposed port from the Dockerfile `EXPOSE 10000`. If you change the port, update `app.py`, the Dockerfile, and Render together.
3. **Environment Variables** (Render dashboard → the service → Environment): set the three Cloudinary keys. Do **not** upload `.env` — Render injects these directly.
   ```
   CLOUDINARY_CLOUD_NAME
   CLOUDINARY_API_KEY
   CLOUDINARY_API_SECRET
   ```
4. **Deploy:** pushing to `main` triggers an auto-deploy. After changing env vars, redeploy (Render prompts, or use "Manual Deploy → Deploy latest commit").
5. **Free-tier note:** low-RAM instances are why the video is encoded at 1 FPS (see Architecture). Large audio files or higher FPS can OOM the instance.

## Architecture notes

- **Everything runs in `fabricar_activo()`.** The whole pipeline is one request handler wrapped in a single try/except that returns `{"status": "error", "mensaje": <exc>}` with HTTP 500 on any failure. Add new steps inside this function's numbered sequence.
- **Files are written to the working directory with fixed names** (`audio_recibido.mp3`, `imagen_base.jpg`, `miniatura_final.jpg`, `video_final.mp4`) and overwritten on every request. This means the service is **not safe for concurrent requests** — parallel calls will clobber each other's intermediate files. Preserve or fix this constraint intentionally.
- **Font path is relative** (`Anton-Regular.ttf`); the container's `WORKDIR /app` + `COPY . .` keeps it alongside `app.py`. Keep the font bundled in the repo.
- **`ffmpeg` is a hard runtime dependency** for moviepy — it is installed via the Dockerfile (`apt-get install ffmpeg`), not pip. Local runs need it on PATH.
- **Thumbnail layout is proportional** to the source image dimensions (font size `= width * 0.07`, wrap at 14 chars, start at 5%/15% of width/height), so it adapts to any image size rather than assuming fixed pixels.
- Cloudinary credentials are read from environment variables (`CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET`), loaded via `python-dotenv` from a gitignored `.env`. See `.env.example` for the required keys. Never hardcode credentials back into `app.py`.
- Port **10000** is fixed in both `app.py` and the Dockerfile `EXPOSE` — change both together.
