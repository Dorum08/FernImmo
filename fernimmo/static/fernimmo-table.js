/* FernImmo – Ergebnistabelle: Sortieren, Spaltenauswahl, Vollbild, Merken, Ausblenden, gesehen. */
(() => {
  "use strict";
  if (window.__fiTable) return;           // Skript nur einmal pro Seite aktiv
  window.__fiTable = true;
  const COLS_KEY = "fernimmo-cols-v1";

  const post = (url, data, json) => fetch(url, {
    method: "POST",
    headers: json ? {"Content-Type": "application/json", Accept: "application/json"} : {Accept: "application/json"},
    body: json ? JSON.stringify(data) : data,
  }).then(r => { if (!r.ok) throw new Error(r.status); return r.json(); });

  document.querySelectorAll(".table-shell").forEach(shell => {
    const table = shell.querySelector("table.results");
    const tools = shell.previousElementSibling;
    if (!table) return;

    // ---- Spalten ein-/ausblenden (Auswahl bleibt im Browser gespeichert) ----
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(COLS_KEY) || "null"); } catch (e) {}
    const boxes = tools ? [...tools.querySelectorAll("input[data-col]")] : [];
    const apply = () => boxes.forEach(b => table.classList.toggle("hide-" + b.dataset.col, !b.checked));
    boxes.forEach(b => {
      if (saved && b.dataset.col in saved) b.checked = saved[b.dataset.col];
      b.addEventListener("change", () => {
        const st = {}; boxes.forEach(x => st[x.dataset.col] = x.checked);
        try { localStorage.setItem(COLS_KEY, JSON.stringify(st)); } catch (e) {}
        apply();
      });
    });
    apply();

    // ---- Sortieren per Klick auf die Kopfzeile ----
    const ths = [...table.querySelectorAll("th.sortable")];
    ths.forEach(th => th.addEventListener("click", () => {
      const idx = [...th.parentNode.children].indexOf(th);
      const dir = th.classList.contains("asc") ? -1 : 1;
      ths.forEach(x => x.classList.remove("asc", "desc"));
      th.classList.add(dir === 1 ? "asc" : "desc");
      const num = th.dataset.kind === "num";
      const rows = [...table.tBodies[0].rows].filter(r => !r.classList.contains("empty"));
      const val = r => r.cells[idx]?.dataset.v ?? "";
      rows.sort((a, b) => {
        const x = val(a), y = val(b);
        if (x === "" && y !== "") return 1;      // leere Werte immer ans Ende
        if (y === "" && x !== "") return -1;
        return (num ? parseFloat(x) - parseFloat(y) : x.localeCompare(y, "de")) * dir;
      });
      rows.forEach(r => table.tBodies[0].appendChild(r));
    }));

    // ---- Aktionen in den Zeilen ----
    table.addEventListener("click", async e => {
      const btn = e.target.closest("button[data-act]");
      if (!btn) return;
      const tr = btn.closest("tr"), id = tr.dataset.id;
      try {
        if (btn.dataset.act === "like") {
          const r = await post(`/listing/${id}/like`, new FormData());
          btn.classList.toggle("on", r.liked);
          btn.textContent = r.liked ? "★" : "☆";
          btn.title = "Merken" + (r.likes.length ? ` (${r.likes.join(", ")})` : "");
        } else if (btn.dataset.act === "hide") {
          const hidden = btn.textContent.trim() === "↩";
          const fd = new FormData(); fd.append("status", hidden ? "neu" : "ausgeblendet");
          await post(`/listing/${id}/status`, fd);
          tr.classList.add("fade-out");
          setTimeout(() => tr.remove(), 250);
        }
      } catch (err) { alert("Aktion fehlgeschlagen (" + err.message + ")"); }
    });

    // ---- Werkzeuge ----
    tools?.querySelector("[data-act=seen-all]")?.addEventListener("click", async ev => {
      const ids = [...table.querySelectorAll("tr.is-new")].map(r => +r.dataset.id);
      await post("/api/seen", {ids}, true);
      table.querySelectorAll("tr.is-new").forEach(r => r.classList.replace("is-new", "is-seen"));
      ev.target.remove();
      tools.querySelector(".new-count")?.remove();
    });
    tools?.querySelector("[data-act=fullscreen]")?.addEventListener("click", () => {
      shell.classList.toggle("fullscreen");
      document.body.classList.toggle("no-scroll", shell.classList.contains("fullscreen"));
    });
    document.addEventListener("keydown", e => {
      if (e.key === "Escape" && shell.classList.contains("fullscreen")) {
        shell.classList.remove("fullscreen"); document.body.classList.remove("no-scroll");
      }
    });
  });
})();
