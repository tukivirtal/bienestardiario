FROM python:3.10-slim
RUN apt-get update && apt-get install -y ffmpeg fonts-liberation
WORKDIR /app
COPY . .
RUN pip install -r requirements.txt
EXPOSE 10000
CMD ["python", "app.py"]
