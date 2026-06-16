FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Install openssl so entrypoint can generate self-signed certs
RUN apt-get update && apt-get install -y --no-install-recommends openssl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY main.py ./
COPY protocol ./protocol
COPY entrypoint.sh ./
RUN chmod +x ./entrypoint.sh

EXPOSE 8080

ENTRYPOINT ["/bin/sh", "./entrypoint.sh"]
