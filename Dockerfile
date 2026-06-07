FROM python:3.12-slim

# Chromium para generar el PDF + fuentes
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium fonts-liberation fonts-dejavu ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ENV CHROME_PATH=/usr/bin/chromium

WORKDIR /app
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY . .
WORKDIR /app/backend

# El host (Render/Railway/etc.) define $PORT
ENV PORT=8000
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT}
