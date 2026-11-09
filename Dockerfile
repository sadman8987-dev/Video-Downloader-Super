FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY main.py downloader.py ./

RUN useradd --create-home --uid 10001 botuser \
    && mkdir -p /tmp/video_bot \
    && chown -R botuser:botuser /app /tmp/video_bot

USER botuser

CMD ["python", "main.py"]
