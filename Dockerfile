# ============================================================================
# Dockerfile — голосовой бот v4.0
# Оптимизирован для Render.com (Free / Starter)
# ============================================================================

# ── Базовый образ ─────────────────────────────────────────────────────────────
# python:3.11-slim: ~150 МБ, без лишних пакетов
FROM python:3.11-slim

# ── Метаданные ────────────────────────────────────────────────────────────────
LABEL maintainer="voice-bot"
LABEL description="Telegram voice bot with FastAPI + Silero/WebRTC VAD"

# ── Системные зависимости ─────────────────────────────────────────────────────
# Устанавливаем в один слой, чистим кэш сразу — минимизирует размер образа
RUN apt-get update && apt-get install -y --no-install-recommends \
    # ffmpeg — конвертация аудио/видео, ОБЯЗАТЕЛЬНО
    ffmpeg \
    # Для webrtcvad нужен компилятор C
    gcc \
    # Для корректной работы с SSL (openai, aiogram)
    ca-certificates \
    # Утилиты для диагностики (можно убрать в продакшне)
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── Рабочая директория ────────────────────────────────────────────────────────
WORKDIR /app

# ── Python зависимости ────────────────────────────────────────────────────────
# Копируем ТОЛЬКО requirements.txt сначала — Docker кэширует этот слой.
# Зависимости переустанавливаются только при изменении requirements.txt,
# не при каждом изменении кода.
COPY requirements.txt .

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ── Исходный код ──────────────────────────────────────────────────────────────
# Копируем после зависимостей — изменения кода не инвалидируют pip-слой
COPY main.py .
COPY handlers.py .
COPY processors.py .
COPY config.py .

# ── Временная папка ───────────────────────────────────────────────────────────
# /tmp уже есть в системе, но явно создаём на случай нестандартного TEMP_DIR
RUN mkdir -p /tmp && chmod 777 /tmp

# ── Не запускать от root ──────────────────────────────────────────────────────
# Хорошая практика безопасности
RUN useradd -m -u 1000 botuser
USER botuser

# ── Переменные окружения ──────────────────────────────────────────────────────
# Значения по умолчанию — переопределяются через Render Environment Variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    # VAD бэкенд: webrtc (default, ~5 МБ) | silero (~250 МБ) | none
    VAD_BACKEND=webrtc

# ── Порт ─────────────────────────────────────────────────────────────────────
EXPOSE 8080

# ── Health check (для Render / Docker) ───────────────────────────────────────
# Render проверяет /health каждые 30с; если 3 раза подряд fail — рестарт
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:${PORT}/health || exit 1

# ── Точка входа ───────────────────────────────────────────────────────────────
# workers=1 — ВАЖНО: polling-режим не совместим с несколькими worker-процессами
CMD ["python", "-m", "uvicorn", "main:app", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--workers", "1", \
     "--log-level", "info"]
