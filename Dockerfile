FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/var/data

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /var/data/output /var/data/uploads /var/data/review_rules

CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:${PORT:-5001} --workers 1 --threads 8 --timeout 300"]
