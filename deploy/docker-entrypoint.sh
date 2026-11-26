#!/bin/sh
# Rechte für den Datenordner herstellen (Docker legt ./data sonst als root an) und
# FernImmo danach als eingeschränkter Nutzer "fernimmo" starten.
set -e
if [ "$(id -u)" = "0" ]; then
  mkdir -p /app/data
  chown -R fernimmo:fernimmo /app/data
  exec setpriv --reuid=fernimmo --regid=fernimmo --init-groups "$@"
fi
exec "$@"
