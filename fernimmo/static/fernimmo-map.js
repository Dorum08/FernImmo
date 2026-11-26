/* FernImmo – gemeinsamer Kartencode (Leaflet). */
"use strict";

const FI = {
  colors: {voll: "#15803d", teilweise: "#d97706", none: "#9ca3af", search: "#2563eb", fw: "#dc2626",
           plzSel: "#2563eb"},

  map(el, center, zoom) {
    const map = L.map(el, {zoomControl: true}).setView(center, zoom || 12);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>-Mitwirkende',
    }).addTo(map);
    return map;
  },

  async json(url, opts) {
    const r = await fetch(url, Object.assign({headers: {"Content-Type": "application/json"}}, opts || {}));
    if (!r.ok) throw new Error(url + ": " + r.status + " " + (await r.text()));
    return r.json();
  },

  _plz: null,
  async plz() {
    if (!this._plz) this._plz = await this.json("/static/plz_nuernberg.geojson");
    return this._plz;
  },

  esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  },

  num(v, unit) {
    if (v == null) return "?";
    return Math.round(v).toLocaleString("de-DE") + (unit || "");
  },

  /** Gezeichnete Gebiete (Suchgebiet blau, Fernwärme rot gestrichelt) als Layer. */
  areaLayer(area, opts) {
    const fw = area.kind === "fw";
    if (area.radius_km != null) {
      // Umkreis: echter Kreis plus Mittelpunkt
      const color = fw ? this.colors.fw : this.colors.search;
      const g = L.featureGroup([
        L.circle([area.center_lat, area.center_lon], Object.assign({radius: area.radius_km * 1000, color, weight: 2,
          dashArray: fw ? "6 4" : null, fillOpacity: fw ? 0.08 : 0.1}, opts || {})),
        L.circleMarker([area.center_lat, area.center_lon], {radius: 5, color: "#fff", weight: 2, fillColor: color,
          fillOpacity: 1, interactive: false}),
      ]);
      return g;
    }
    return L.geoJSON(area.geometry, Object.assign({
      style: {color: fw ? this.colors.fw : this.colors.search, weight: 2, dashArray: fw ? "6 4" : null,
              fillOpacity: fw ? 0.08 : 0.12},
    }, opts || {}));
  },

  /**
   * Freihand-Zeichnen: Maus/Finger gedrückt halten und Fläche umfahren.
   * onDone(GeoJSON-Polygon) wird nach dem Loslassen aufgerufen.
   */
  freehand(map, color, onDone, onCancel) {
    const el = map.getContainer();
    const pts = [];
    let line = null, down = false;
    map.dragging.disable();
    map.doubleClickZoom.disable();
    if (map.tap) map.tap.disable();
    el.classList.add("drawing");
    const ll = e => map.mouseEventToLatLng(e);
    const stop = () => {
      el.removeEventListener("pointerdown", onDown);
      el.removeEventListener("pointermove", onMove);
      el.removeEventListener("pointerup", onUp);
      document.removeEventListener("keydown", onKey);
      el.classList.remove("drawing");
      map.dragging.enable();
      map.doubleClickZoom.enable();
      if (map.tap) map.tap.enable();
      if (line) map.removeLayer(line);
    };
    const onDown = e => {
      if (e.button !== undefined && e.button !== 0) return;
      down = true;
      el.setPointerCapture(e.pointerId);
      pts.length = 0;
      pts.push(ll(e));
      line = L.polyline(pts, {color, weight: 3}).addTo(map);
      e.preventDefault();
    };
    const onMove = e => {
      if (!down) return;
      const p = ll(e);
      const last = map.latLngToContainerPoint(pts[pts.length - 1]);
      if (last.distanceTo(map.latLngToContainerPoint(p)) >= 4) {   // Punkte ausdünnen
        pts.push(p);
        line.setLatLngs(pts);
      }
      e.preventDefault();
    };
    const onUp = () => {
      if (!down) return;
      down = false;
      stop();
      if (pts.length < 4) { if (onCancel) onCancel(); return; }
      const ring = pts.map(p => [Math.round(p.lng * 1e5) / 1e5, Math.round(p.lat * 1e5) / 1e5]);
      ring.push(ring[0]);
      onDone({type: "Polygon", coordinates: [ring]});
    };
    const onKey = e => { if (e.key === "Escape") { stop(); if (onCancel) onCancel(); } };
    el.addEventListener("pointerdown", onDown);
    el.addEventListener("pointermove", onMove);
    el.addEventListener("pointerup", onUp);
    document.addEventListener("keydown", onKey);
    return stop;
  },
};
