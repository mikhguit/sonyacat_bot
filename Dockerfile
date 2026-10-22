FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MALLOC_ARENA_MAX=2

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .

RUN useradd --create-home --uid 10001 bot \
    && mkdir -p /app/data && chown -R bot:bot /app/data
USER bot
# Файлы данных (balance.json, users.json). Чтобы они переживали перезапуски — смонтируйте сюда постоянный том
# (или укажите DATA_DIR = путь, куда хостинг монтирует volume).
ENV DATA_DIR=/app/data

CMD ["python", "bot.py"]
