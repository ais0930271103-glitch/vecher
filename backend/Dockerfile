FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# COPY сохраняет права checkout: код должен читаться и после umask 077 на хосте.
RUN chmod -R a+rX /srv/app \
    && useradd --create-home --uid 10001 vecher
USER vecher

EXPOSE 8000
# Один процесс: внутри работает планировщик писем/напоминаний — несколько воркеров дублировали бы уведомления.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
