# FernImmo

Selbst gehosteter Wohnungs-Suchagent nach dem Vorbild von Immometrica. FernImmo
durchsucht regelmäßig Immobilienportale, liest aus jedem Inserat die Merkmale aus
(Größe, Zimmer, Preis, Baujahr, Heizung/Energieträger, Energieklasse, Etage,
Ausstattung …), gleicht sie mit deinen Suchprofilen ab und meldet passende
Angebote per Telegram, ntfy oder E-Mail. Eine Weboberfläche zeigt alle Inserate
mit Filtern, Preisverlauf und Merkliste.

FernImmo funktioniert in ganz Deutschland. Die Beispielkonfiguration sucht
**1–2-Zimmer-Eigentumswohnungen (Kauf) in Nürnberg mit Fernwärme**; Gebiet und
Zimmerzahl stellst du in der Weboberfläche unter „Suche“ um.

## Funktionen

| | |
|---|---|
| **Quellen** | Eigene Adapter: Kleinanzeigen, Immowelt, ImmoScout24 (optional, siehe unten). Lernfähiger Adapter: Ohne-Makler, Immobilo, Immobilien.de, meinestadt.de, IVD24, RegionalImmobilien24, 1A-Immobilienmarkt und beliebige weitere Seiten. Siehe „Portale“. |
| **Zimmer** | Erkennt auch halbe Zimmer in allen üblichen Schreibweisen (2,5 · 2.5 · 2½ · 2 1/2 · zweieinhalb) und ausgeschriebene Zahlen (Zweizimmerwohnung). Zimmer von/bis in 0,5er-Schritten unter „Suche“ (gilt für Meldungen) und als Filter in Übersicht und Karte. |
| **Auswertung** | Wohnfläche, Zimmer, Kalt-/Warmmiete, Nebenkosten, Heizkosten, Kaufpreis, Hausgeld, €/m², Baujahr, Energieträger (Fernwärme, Nahwärme, Gas, Öl, Wärmepumpe, Pellets, Strom, BHKW), Heizungsart, Energieklasse und Kennwert, Etage, PLZ, „verfügbar ab“, Ausstattung (Balkon, EBK, Aufzug, Keller, Stellplatz …) |
| **Hinweise & Ausschlüsse** | Gesuche, Zwangsversteigerungen (standardmäßig ausgeschlossen); erkannt und angezeigt werden außerdem Erbpacht, vermietete Wohnungen, Denkmalschutz, provisionsfrei; eigene Stichwörter möglich |
| **Treffer-Stufen** | **voll**: alle Kriterien erfüllt und Fernwärme bestätigt. **teilweise**: nichts spricht dagegen, aber z. B. die Heizung steht nicht im Inserat. Wird ein anderer Energieträger genannt, fällt das Inserat raus. |
| **IDs, Likes, Kommentare** | Jede Wohnung hat eine feste FernImmo-ID (#12; auch in den Meldungen, über die Suche direkt aufrufbar). Auf der Übersicht kann jede angemeldete Person liken und kommentieren; man sieht, wer was gelikt und geschrieben hat. |
| **Änderungsverlauf** | Treffer und gelikte Wohnungen werden regelmäßig neu abgerufen. Jede Änderung (Kaufpreis, Hausgeld, Fläche, Energiedaten, Titel, Beschreibung, offline/wieder online …) wird mit Datum und Uhrzeit auf der Detailseite protokolliert, Preissenkungen grün, Erhöhungen rot. |
| **Meldungen** | Telegram, ntfy (Push aufs Handy ohne Konto), E-Mail; Preissenkungen bei gemeldeten oder gelikten Wohnungen |
| **Suchgebiet** | Auf der Seite „Suche“: Ort in ganz Deutschland anspringen, PLZ per Klick auf der Karte auswählen oder als PLZ/PLZ-Anfang eingeben (z. B. „904“), Flächen frei mit Maus oder Finger einzeichnen und/oder eine **Umkreissuche** anlegen (Ort per Adresse, PLZ oder Klick in die Karte, Radius 0,5–20 km per Schieberegler). Alles ist beliebig kombinierbar. Bei Umkreisen zeigt die Übersicht die Luftlinie zum Mittelpunkt und kann danach sortieren. Zusätzlich lassen sich Fernwärme-Gebiete einzeichnen: Steht im Inserat keine Heizung, liegt die Wohnung aber darin, wird das angezeigt. Nach jeder Änderung werden alle Inserate automatisch neu bewertet. |
| **Karte** | Alle Inserate (mit den Filtern der Übersicht) auf einer OpenStreetMap-Karte, farbig nach Treffer, mit Suchgebiet und Infos per Klick. |
| **Marktvergleich** | €/m² jeder Wohnung im Vergleich zum Median ihrer PLZ (ab 3 Vergleichsinseraten, sonst über alle). Die Vergleichsbasis sind die Inserate, die FernImmo im letzten Jahr gesammelt hat; sie wird also mit der Zeit besser. |
| **Angebotsdauer** | Tage am Markt, Anzahl Preissenkungen und Preisänderung seit dem ersten Preis; Sortierung „am längsten online“. |
| **Wiedereinstellungen** | Wird eine Wohnung gelöscht und neu eingestellt (gleiche PLZ, Fläche, Zimmer), verknüpft FernImmo beide. Angebotsdauer und Verlauf zählen ab dem ersten Inserat, die Meldung sagt „wieder eingestellt“. |
| **Kostenrechner** | Pro Wohnung: Kaufnebenkosten mit der Grunderwerbsteuer des jeweiligen Bundeslands, Darlehen, Monatsrate, monatliche Belastung inkl. Hausgeld, Brutto-/Nettomietrendite und Kaufpreisfaktor. Werte im Browser änderbar; die Monatsbelastung steht auch in Übersicht und Meldung. |
| **Export** | Aktuelle Liste (mit allen Filtern) als Excel- oder CSV-Datei, inklusive Likes, Kommentaren und Kennzahlen. |
| **Dubletten** | Dasselbe Objekt auf mehreren Portalen wird erkannt (PLZ, Fläche, Preis, Zimmer) und nur einmal gemeldet |
| **Weboberfläche** | Filter (u. a. „nur gelikte“), Sortierung nach Preis, €/m², Likes oder letzter Änderung, Status (Favorit, kontaktiert, Besichtigung, ausgeblendet), Laufprotokoll, „Jetzt suchen“-Knopf, JSON-API unter `/api/listings` und `/api/listings/<id>/changes` |

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

**Mehrere Personen** (z. B. für gemeinsames Liken und Kommentieren): in `config.yaml`
unter `web.users` weitere Namen eintragen, etwa `zweite_person: ${WEB_PASSWORD_2}`,
und das Passwort in `.env` setzen. Likes und Kommentare werden dem angemeldeten
Namen zugeordnet; eigene Kommentare lassen sich löschen.

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

- `listing_types: [kauf]`: nur Kaufangebote (`[miete]` oder `[miete, kauf]` geht auch; dann
  auch die Such-URLs der Portale entsprechend anpassen)
- `rooms_min`, `rooms_max`: 1 bis 2 (mit `2.5` werden 2,5-Zimmer-Wohnungen eingeschlossen)
- `price_max`: maximaler Kaufpreis

  Zimmer, Kaufpreis und Mindestfläche lassen sich bequemer in der Weboberfläche unter
  „Suche → Suchkriterien“ einstellen. Diese Werte gelten dann für alle Profile.
- `heating_required: [fernwaerme]`: mit `[fernwaerme, nahwaerme]` zählt auch Nahwärme
- `unknown_heating: teilweise`: Inserate ohne Heizungsangabe trotzdem melden (gelb). In der Praxis
  fehlt die Angabe oft, besonders bei Kleinanzeigen. Mit `ignorieren` bekommst du nur bestätigte Fernwärme.
- `plz: ["904"]`: PLZ oder PLZ-Anfänge, solange kein Suchgebiet in der Weboberfläche festgelegt ist
- `exclude_flags`: z. B. `[gesuch, versteigerung, erbpacht, vermietet]`; dazu `exclude_keywords`, `required_features`

## Lage und Karte

FernImmo bestimmt die Lage jeder Wohnung so genau wie möglich:

1. **laut Portal:** Koordinaten, die das Portal in der Seite mitliefert
2. **aus der Adresse:** Steht eine Straße im Inserat, wird sie über OpenStreetMap
   (Nominatim) in Koordinaten umgerechnet. Das passiert höchstens einmal pro Sekunde
   und wird gespeichert. Abschalten mit `geocoding: enabled: false`.
3. **nur PLZ:** Mittelpunkt des PLZ-Gebiets

Liegt eine Wohnung nur PLZ-genau vor und überschneidet das PLZ-Gebiet eine
gezeichnete Fläche, gilt die Lage als **unklar**. Die Wohnung wird dann als
möglicher Treffer gemeldet statt verworfen.

Alle rund 8.200 deutschen PLZ-Gebiete liegen fertig im Projekt
(`fernimmo/static/plz/`, © OpenStreetMap-Mitwirkende, ODbL, Stand Februar 2026),
dazu je PLZ das Bundesland für die Grunderwerbsteuer. Bei PLZ auf einer Landesgrenze
entscheidet die Lage des Mittelpunkts; im Kostenrechner lässt sich der Satz ändern.
Die Karte lädt PLZ-Gebiete nur für den sichtbaren Ausschnitt nach. Die Kartenkacheln
holt dein Browser direkt von openstreetmap.org.

## Portale

| Portal | Adapter | Hinweis |
|---|---|---|
| Kleinanzeigen | eigener | liest auch die Detail-Liste (Zimmer, Fläche, Heizung …) |
| Immowelt | eigener | Immonet läuft auf derselben Plattform |
| ImmoScout24 | eigener, **aus** | starker Bot-Schutz, nur mit `fetcher: playwright` einen Versuch wert |
| Ohne-Makler | lernfähig | |
| Immobilo | lernfähig | Meta-Suche; Inserate von Immowelt/Kleinanzeigen liest der passende eigene Adapter |
| Immobilien.de | lernfähig | |
| meinestadt.de | lernfähig | Immobilienteil zeigt überwiegend Immowelt-Inserate, Weiterleitung wie bei Immobilo |
| IVD24 (ivd24immobilien.de) | lernfähig | |
| RegionalImmobilien24 | lernfähig | |
| 1A-Immobilienmarkt | lernfähig | |
| beliebige Seite | lernfähig (`type: generic`) | z. B. Wohnungsbaugesellschaft oder Makler |

**Der lernfähige Adapter** braucht keinen portalspezifischen Code. Auf der
Ergebnisseite sucht er das Link-Muster, das mit Objektnummer am häufigsten vorkommt
(z. B. `/expose/{Nummer}/…`). Auf der Detailseite liest er Text, „Label: Wert“-Paare,
schema.org-Daten und Koordinaten. Ähnliche-Objekte-Blöcke blendet er aus. Erkennt
er auf einem Portal das falsche Muster, kannst du es mit `link_pattern` fest
vorgeben; die Inhalte grenzt `content_selector` ein.

**So richtest du ein Portal ein:** Suche im Browser einstellen (Ort oder Region,
Wohnung kaufen, möglichst „neueste zuerst“), die URL aus der Adresszeile in
`config.yaml` unter `urls` eintragen, neu starten. Nach dem ersten Lauf ein Inserat
mit `python -m fernimmo check <Inserat-URL> --source <name>` prüfen. Ohne URL ist
ein Portal aus. Mehrere URLs pro Portal (z. B. mehrere Städte) sind möglich.

**Deutschlandweit suchen:** Bei Kleinanzeigen etwa die bundesweite Kategorie
„Wohnung kaufen“, sortiert nach neuesten. Damit nicht jedes Inserat einzeln geöffnet
werden muss, prüft FernImmo schon die Kurzinfo in der Ergebnisliste (PLZ, Zimmer)
gegen Suchgebiet und Kriterien und lädt nur Detailseiten, die passen könnten.
Bundesweite Listen ändern sich schnell; je kleiner die Region in der Such-URL,
desto weniger Inserate verpasst FernImmo zwischen zwei Läufen.

## Grenzen und Hinweise

- **Nicht live getestet:** Alle Adapter wurden mit nachgebauten Seiten entwickelt und
  getestet, aber nicht gegen die echten Portale, weil diese aus der
  Entwicklungsumgebung nicht erreichbar waren. Beim ersten Einsatz bitte mit `check` prüfen.
- **Bot-Schutz:** Portale ändern ihr HTML und wehren automatisierte Abrufe ab.
  Kleinanzeigen und Immowelt funktionieren meist mit dem einfachen HTTP-Abruf.
  **ImmoScout24** blockt diesen fast immer und ist deshalb standardmäßig aus. Mit
  `fetcher: playwright` (Docker: `PLAYWRIGHT: "1"`) lädt FernImmo die Seiten in einem
  echten Headless-Browser; das klappt manchmal. Blockaden erscheinen im Laufprotokoll
  und als Hinweis auf der Startseite.
- **Wenn ein Portal sein Layout ändert,** prüfe mit `python -m fernimmo check <Inserat-URL>`,
  was erkannt wird. Die Selektoren stehen in `fernimmo/sources/*.py`.
- **Änderungsverlauf:** Erfasst wird, was beim erneuten Abruf anders ist. Neu geladen
  werden Treffer und gelikte Wohnungen, spätestens alle `refresh_hours` Stunden (Standard 6)
  und höchstens `refresh_per_run` pro Lauf. Ist ein Inserat gelöscht oder lange nicht mehr
  in der Suche, wird es als „offline“ vermerkt (Filter „auch offline“).
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
  sources/      Portal-Adapter (Suche durchblättern, Detailseite zerlegen);
                generic.py = lernfähiger Adapter für weitere Portale
  extract.py    Merkmalserkennung aus Text und Label-Wert-Paaren
  matcher.py    Abgleich mit Suchprofilen (voll / teilweise / kein Treffer)
  pipeline.py   Suchlauf, Speichern, Melden, Zeitplan
  db.py         SQLite (Inserate, Änderungsverlauf, Likes, Kommentare, Gebiete, Läufe)
  geo.py        Koordinaten, Geocoding, PLZ-Gebiete, Punkt-in-Fläche, Suchgebiet
  calc.py       Kostenrechner (Kaufnebenkosten, Finanzierung, Rendite)
  static/       Leaflet (Karte), PLZ-Grenzen ganz Deutschland (plz/), Kartencode
  notify.py     Telegram / ntfy / E-Mail
  web.py        Weboberfläche (FastAPI + Jinja)
```
