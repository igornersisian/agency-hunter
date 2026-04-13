#!/bin/bash
set -e

# Decode Gmail OAuth files from base64 env vars (set in Dokploy UI)
if [ -n "$GMAIL_CREDENTIALS_B64" ]; then
    echo "$GMAIL_CREDENTIALS_B64" | base64 -d > /app/credentials.json
    export GMAIL_OAUTH_CREDENTIALS_PATH=/app/credentials.json
    echo "Decoded credentials.json"
fi

if [ -n "$GMAIL_TOKEN_B64" ]; then
    echo "$GMAIL_TOKEN_B64" | base64 -d > /app/token.json
    export GMAIL_TOKEN_PATH=/app/token.json
    echo "Decoded token.json"
fi

exec "$@"
