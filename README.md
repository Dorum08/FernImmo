# FernImmo

Selbst gehosteter Wohnungs-Suchagent nach dem Vorbild von Immometrica. FernImmo
durchsucht regelmäßig Immobilienportale, liest aus jedem Inserat die Merkmale aus
(Größe, Zimmer, Preis, Baujahr, Heizung/Energieträger, Energieklasse, Etage,
Ausstattung …), gleicht sie mit deinen Suchprofilen ab und meldet passende
Angebote per Telegram, ntfy oder E-Mail. Eine Weboberfläche zeigt alle Inserate
mit Filtern, Preisverlauf und Merkliste.

Voreingestellt ist die Suche nach **1–2-Zimmer-Wohnungen in Nürnberg mit Fernwärme**.

## Funktionen

| | |
|---|---|
| **Quellen** | Kleinanzeigen, Immowelt, ImmoScout24 (optional, siehe unten), beliebige weitere Seiten über ein Link-Muster |
| **Auswertung** | Wohnfläche, Zimmer, Kalt-/Warmmiete, Nebenkosten, Heizkosten, Kaufpreis, Hausgeld, €/m², Baujahr, Energieträger (Fernwärme, Nahwärme, Gas, Öl, Wärmepumpe, Pellets, Strom, BHKW), Heizungsart, Energieklasse und Kennwert, Etage, PLZ, „verfügbar ab“, Ausstattung (Balkon, EBK, Aufzug, Keller, Stellplatz …) |
| **Ausschlüsse** | Tauschwohnungen, WG-Zimmer, Gesuche, Zwischenmiete/befristet, WBS, Seniorenwohnen, eigene Stichwörter |
| **Treffer-Stufen** | **voll**: alle Kriterien erfüllt und Fernwärme bestätigt. **teilweise**: nichts spricht dagegen, aber z. B. die Heizung steht nicht im Inserat. Wird ein anderer Energieträger genannt, fällt das Inserat raus. |
| **Meldungen** | Telegram, ntfy (Push aufs Handy ohne Konto), E-Mail; Preissenkungen bereits gemeldeter Inserate |
| **Dubletten** | Dasselbe Objekt auf mehreren Portalen wird erkannt (PLZ, Fläche, Preis, Zimmer) und nur einmal gemeldet |
| **Weboberfläche** | Filter, Sortierung nach Preis und €/m², Status (Favorit, kontaktiert, Besichtigung, ausgeblendet), Preisverlauf, Laufprotokoll, „Jetzt suchen“-Knopf, JSON-API unter `/api/listings` |

## Installation mit Docker (empfohlen)

```bash
git clone <dieses-repo> fernimmo && cd fernimmo
cp config.example.yaml config.yaml
cp .env.example .env          # Passwort und Benachrichtigungen eintragen
docker compose up -d --build
docker compose logs -f
```

Die Weboberfläche läuft dann auf `http://127.0.0.1:8080` auf dem Server. Sie ist
absichtlich nur lokal gebunden. Von außen erreichst du sie per SSH-Tunnel
(`ssh -L 8080:127.0.0.1:8080 dein-server`), VPN (z. B. WireGuard oder Tailscale)
oder über einen Reverse-Proxy mit HTTPS (Caddy, nginx, Traefik). Setze in `.env`
auf jeden Fall `WEB_PASSWORD`.

Nützliche Befehle:

```bash
docker compose exec fernimmo python -m fernimmo -c /app/config.yaml test-notify   # Testnachricht
docker compose exec fernimmo python -m fernimmo -c /app/config.yaml run           # Lauf sofort
docker compose exec fernimmo python -m fernimmo -c /app/config.yaml check <URL>   # ein Inserat prüfen
docker compose exec fernimmo python -m fernimmo -c /app/config.yaml reprocess     # nach Profiländerung neu bewerten
```

Nach Änderungen an `config.yaml`: `docker compose restart`.

## Ohne Docker

```bash
sudo useradd -r -m -d /opt/fernimmo fernimmo
sudo -u fernimmo git clone <dieses-repo> /opt/fernimmo
cd /opt/fernimmo
sudo -u fernimmo python3 -m venv .venv
sudo -u fernimmo .venv/bin/pip install -r requirements.txt
sudo -u fernimmo cp config.example.yaml config.yaml
sudo -u fernimmo cp .env.example .env     # ausfüllen
sudo cp deploy/fernimmo.service /etc/systemd/system/
sudo systemctl enable --now fernimmo
journalctl -u fernimmo -f
```

## Benachrichtigungen einrichten

**Telegram** (empfohlen, mit Link-Vorschau):
1. In Telegram `@BotFather` anschreiben, `/newbot` und den Token kopieren.
2. Dem neuen Bot eine Nachricht schicken.
3. `https://api.telegram.org/bot<TOKEN>/getUpdates` öffnen; bei `"chat":{"id":…}` steht deine Chat-ID.
4. In `.env`: `TELEGRAM_ENABLED=true`, Token und Chat-ID eintragen.

**ntfy** (ohne Konto): die App „ntfy“ installieren, ein langes, zufälliges Topic
abonnieren und in `.env` `NTFY_ENABLED=true` und `NTFY_TOPIC=…` setzen. Wer das
Topic kennt, kann mitlesen, deshalb einen schwer zu erratenden Namen wählen.

**E-Mail**: SMTP-Zugang deines Mailanbieters eintragen (Port 587 mit STARTTLS oder 465 mit SSL).

Beim allerersten Lauf je Quelle gibt es nur eine Zusammenfassung statt dutzender
Einzelmeldungen für den Altbestand. Das lässt sich mit `notify.first_run: true` ändern.

## Suchprofil anpassen

Alles steht in `config.yaml` unter `profiles`. Mehrere Profile sind möglich, zum
Beispiel eines für Miete und eines für Kauf. Wichtige Schalter:

- `rooms_min`, `rooms_max`: 1 bis 2 (mit `2.5` werden 2,5-Zimmer-Wohnungen eingeschlossen)
- `price_max`: Kaltmiete bzw. Kaufpreis; `rent_warm_max` für die Warmmiete
- `heating_required: [fernwaerme]`: mit `[fernwaerme, nahwaerme]` zählt auch Nahwärme
- `unknown_heating: teilweise`: Inserate ohne Heizungsangabe trotzdem melden (gelb). In der Praxis
  fehlt die Angabe oft, besonders bei Kleinanzeigen. Mit `ignorieren` bekommst du nur bestätigte Fernwärme.
- `plz: ["904"]`: nur Nürnberger Stadtgebiet
- `exclude_flags`, `exclude_keywords`, `required_features`

## Suchseiten (Quellen-URLs)

Am zuverlässigsten: die Suche auf dem jeweiligen Portal im Browser einstellen
(Nürnberg, Wohnung mieten, eventuell schon Zimmer und Preis) und die URL aus der
Adresszeile in `config.yaml` einfügen. FernImmo blättert bis zu `max_pages` Seiten
durch und öffnet nur Inserate, die es noch nicht kennt. **Bitte die Beispiel-URLs
einmal im Browser prüfen**, ob sie wirklich Nürnberg zeigen; die Orts-IDs der
Portale konnten beim Erstellen nicht live überprüft werden.

Weitere Seiten, etwa von Wohnungsbaugesellschaften, gehen mit `type: generic`
und einem `link_pattern` (Regex für die Exposé-Links, Gruppe 1 = ID). Ein Beispiel
steht in `config.example.yaml`.

## Grenzen und Hinweise

- **Bot-Schutz:** Portale ändern ihr HTML und wehren automatisierte Abrufe ab.
  Kleinanzeigen und Immowelt funktionieren meist mit dem einfachen HTTP-Abruf.
  **ImmoScout24** blockt diesen fast immer und ist deshalb standardmäßig aus. Mit
  `fetcher: playwright` (Docker: `PLAYWRIGHT: "1"`) lädt FernImmo die Seiten in einem
  echten Headless-Browser; das klappt manchmal. Blockaden erscheinen im Laufprotokoll
  und als Hinweis auf der Startseite.
- **Wenn ein Portal sein Layout ändert,** prüfe mit `python -m fernimmo check <Inserat-URL>`,
  was erkannt wird. Die Selektoren stehen in `fernimmo/sources/*.py`.
- **Abrufrate:** Standard ist alle ca. 20 Minuten mit Pausen von 3–7 s zwischen den
  Seiten und Ruhezeit nachts. Kürzere Intervalle erhöhen das Risiko, geblockt zu werden.
- **Rechtliches:** Die Nutzungsbedingungen der Portale verbieten automatisiertes
  Auslesen in der Regel. Eine moderate Nutzung für die eigene private Wohnungssuche
  ist üblich, die Verantwortung liegt aber bei dir. Gib die gesammelten Daten nicht
  weiter und veröffentliche sie nicht.
- **Erkennung ist regelbasiert:** Fernwärme wird aus Strukturfeldern („Heizungsart“,
  „Wesentliche Energieträger“, „Befeuerung“, Kürzel „FW“) und aus dem Freitext
  erkannt. Die Detailseite zeigt jeweils, woher die Angabe stammt. Steht nichts im
  Inserat, lohnt eine kurze Nachfrage beim Anbieter oder ein Blick in den
  Energieausweis.

## Entwicklung

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Aufbau:

```
fernimmo/
  sources/      Portal-Adapter (Suche durchblättern, Detailseite zerlegen)
  extract.py    Merkmalserkennung aus Text und Label-Wert-Paaren
  matcher.py    Abgleich mit Suchprofilen (voll / teilweise / kein Treffer)
  pipeline.py   Suchlauf, Speichern, Melden, Zeitplan
  db.py         SQLite (Inserate, Preisverlauf, Läufe)
  notify.py     Telegram / ntfy / E-Mail
  web.py        Weboberfläche (FastAPI + Jinja)
```
