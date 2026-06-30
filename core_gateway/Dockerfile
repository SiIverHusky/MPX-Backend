FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./
COPY config.py ./
COPY crypto.py ./
COPY openclaw.py ./
COPY protocol ./protocol
COPY entrypoint.sh ./
RUN chmod +x ./entrypoint.sh

EXPOSE 8080

ENTRYPOINT ["/bin/sh", "./entrypoint.sh"]
