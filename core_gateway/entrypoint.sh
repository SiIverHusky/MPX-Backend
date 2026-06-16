#!/bin/sh
set -e
CERT_DIR=/tmp/certs
mkdir -p "$CERT_DIR"
KEY="$CERT_DIR/server.key"
CRT="$CERT_DIR/server.crt"

# Generate a self-signed cert if none provided
if [ ! -f "$KEY" ] || [ ! -f "$CRT" ]; then
  echo "Generating self-signed TLS certificate"
  openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
    -subj "/CN=192.168.50.201" \
    -keyout "$KEY" -out "$CRT"
fi

# Default host/port from env
HOST=${CORE_GATEWAY_HOST:-0.0.0.0}
PORT=${CORE_GATEWAY_PORT:-8080}

exec uvicorn main:app --host "$HOST" --port "$PORT" --workers 1 --loop uvloop --http httptools \
  --ssl-keyfile "$KEY" --ssl-certfile "$CRT"
