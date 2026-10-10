FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    COQUI_TOS_AGREED=1 \
    CLONE_HOST=0.0.0.0 \
    HOME=/app

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
      git ffmpeg libsndfile1 espeak-ng curl && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir \
      --extra-index-url https://download.pytorch.org/whl/cpu \
      -r requirements.txt

# Bake the XTTS-v2 model into the image so cold starts don't re-download ~1.9GB.
RUN python -c "import os; os.environ['COQUI_TOS_AGREED']='1'; from TTS.api import TTS; TTS('tts_models/multilingual/multi-dataset/xtts_v2')"

COPY server.py .

# Cloud Run injects PORT (default 8080); server.py reads it automatically.
EXPOSE 8080
CMD ["python", "server.py"]
