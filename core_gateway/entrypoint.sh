#!/bin/sh
set -e

# Default host/port from env
HOST=${CORE_GATEWAY_HOST:-0.0.0.0}
PORT=${CORE_GATEWAY_PORT:-8080}

exec uvicorn main:app --host "$HOST" --port "$PORT" --workers 1 --loop uvloop --http httptools
