#!/bin/sh
set -e

CERT_DIR=/etc/nginx/certs
SERVER_IP="${SERVER_IP:-192.168.135.168}"

if [ ! -f "$CERT_DIR/cert.pem" ] || [ ! -f "$CERT_DIR/key.pem" ]; then
    echo "[EVEGuru2] Generating self-signed SSL certificate for $SERVER_IP ..."
    mkdir -p "$CERT_DIR"
    openssl req -x509 -nodes \
        -newkey rsa:2048 \
        -days 3650 \
        -keyout "$CERT_DIR/key.pem" \
        -out    "$CERT_DIR/cert.pem" \
        -subj   "/C=GB/ST=England/O=EVEGuru2/CN=$SERVER_IP" \
        -addext "subjectAltName=IP:$SERVER_IP,DNS:localhost"
    echo "[EVEGuru2] Certificate generated — valid for 10 years."
else
    echo "[EVEGuru2] Using existing SSL certificate."
fi

exec nginx -g "daemon off;"
