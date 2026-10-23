# Контейнер работает от root осознанно: БД панели в WAL (root:root 600) читается
# read-only целиком, rootfs контейнера — read-only (см. compose), единственное
# место записи — смонтированный каталог бота. Не-root юзер здесь хрупок: панель
# пересоздаёт x-ui.db-shm/-wal при рестарте и права слетают.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

CMD ["python", "-m", "app.main"]
