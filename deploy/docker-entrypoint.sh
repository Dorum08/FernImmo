#!/bin/sh
# Startskript des Containers:
#  1. Fehlt die Konfiguration (FERNIMMO_CONFIG), wird eine Vorlage angelegt – beim ersten
#     Start mit leerem config-Ordner. Danach nur noch diese Datei bearbeiten.
#  2. Rechte für den Datenordner herstellen (Docker legt eingehängte Ordner sonst als root an).
#  3. FernImmo als eingeschränkter Nutzer "fernimmo" starten.
set -e
CONFIG="${FERNIMMO_CONFIG:-/app/config.yaml}"
if [ ! -e "$CONFIG" ] && [ -d "$(dirname "$CONFIG")" ]; then
  cp /app/config.nas.yaml "$CONFIG"
  echo "FernImmo: Vorlage angelegt unter $CONFIG – bitte public_url und Such-URLs anpassen."
fi
if [ -d "$CONFIG" ]; then
  echo "FEHLER: $CONFIG ist ein Ordner statt einer Datei. Ordner löschen und Container neu starten." >&2
  exit 1
fi
if [ "$(id -u)" = "0" ]; then
  mkdir -p /app/data
  chown -R fernimmo:fernimmo /app/data
  exec setpriv --reuid=fernimmo --regid=fernimmo --init-groups "$@"
fi
exec "$@"
