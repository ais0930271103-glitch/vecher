FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini

# COPY сохраняет права checkout: код должен читаться и после umask 077 на хосте.
RUN chmod -R a+rX /srv/app /srv/alembic /srv/alembic.ini \
    && useradd --create-home --uid 10001 vecher
USER vecher

EXPOSE 8000
# Один процесс по умолчанию; outbox использует DB leases и уникальные ключи между workers.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
