// The mod details panel. Local facts come from the table row (always shown); Nexus data arrives later as a "details" push from Python.
// Everything an author wrote is plain text by the time it gets here and is only ever set with textContent / h().
import { call } from "../api.js";
import { h } from "../dom.js";
import { icon } from "../icons.js";
import { state, watch } from "../store.js";
import { button } from "./widgets.js";

const NOTES = {
  no_key: "Add your Nexus API key in Settings to see details from Nexus.",
  low_quota: "The Nexus request budget is low right now. Details will load later.",
  rate_limited: "Nexus is rate limiting this key. Try again later.",
  not_found: "Nexus has no mod with this id. It may have been removed.",
  offline: "Could not reach Nexus.",
  error: "Nexus did not return usable details.",
};

const number = (n) => (typeof n === "number" ? n.toLocaleString() : "-");
const day = (ts) => (typeof ts === "number" && ts > 0 ? new Date(ts * 1000).toLocaleDateString() : "-");

export function createDetails({ onClose }) {
  let folder = null, timer = null, descOpen = false, verOpen = false;
  const loading = new Set(), versLoading = new Set(), failed = new Map();

  const body = h("div", { class: "d-body" });
  const closeBtn = h("button", { type: "button", class: "d-close", "aria-label": "Close details", title: "Close (Esc)" }, icon("close"));
  closeBtn.addEventListener("click", () => onClose());
  const title = h("h2", { class: "d-title" });
  const el = h("aside", { class: "details", role: "complementary", "aria-label": "Mod details", hidden: true }, h("div", { class: "d-head" }, title, closeBtn), body);
  el.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); onClose(true); } });

  const row = () => state.rows.get(folder);

  function actions(r) {
    const act = (id) => () => call("row_action", folder, id);
    const out = [];
    if (r.linked) {
      if (r.kind === "update") out.push(button({ label: r.latest && r.latest !== "-" ? `Update to ${r.latest}` : "Update", icon: "sync", kind: "primary", onClick: act("update") }));
      else out.push(button({ label: "Install latest", icon: "add", kind: "secondary", title: "Open the Nexus files page for this mod", onClick: act("open_page") }));
      out.push(button({ label: "Open on Nexus", icon: "info", kind: "secondary", onClick: () => call("open_nexus_page", folder) }));
      if (r.rollback) out.push(button({ label: "Roll back", kind: "secondary", title: "Undo the last update", onClick: act("rollback") }));
      out.push(button({ label: r.pinned ? "Unpin" : "Pin", icon: "pin", kind: "ghost", title: "Pin or unpin the installed version", onClick: act("pin") }));
    } else {
      out.push(button({ label: "Link to Nexus", icon: "add", kind: "primary", onClick: act("link") }));
    }
    return h("div", { class: "d-actions" }, ...out);
  }

  function nexusSection(r) {
    const d = state.details.get(folder);
    const data = d && d.data;
    const parts = [];
    if (data) {
      parts.push(d.image ? h("img", { class: "d-hero", src: d.image, alt: "", width: "420", height: "236" }) : h("div", { class: "d-hero ph", "aria-hidden": "true" }));
      if (data.name) parts.push(h("div", { class: "d-name", text: data.name }));
      parts.push(h("div", { class: "d-sub muted", text: [data.author ? `by ${data.author}` : "", data.version ? `v${data.version}` : ""].filter(Boolean).join("  ·  ") }));
      parts.push(h("div", { class: "d-stats" }, h("span", { text: `${number(data.endorsements)} endorsements` }), h("span", { text: `${number(data.downloads)} downloads` }),
        h("span", { text: `updated ${day(data.updated)}` })));
    } else parts.push(h("div", { class: "d-hero ph", "aria-hidden": "true" }));
    if (!data && loading.has(folder)) parts.push(h("div", { class: "d-note", role: "status", text: "Loading from Nexus..." }));
    else if (failed.has(folder) && !data) parts.push(h("div", { class: "d-note bad", role: "status", text: failed.get(folder) }));
    else if (d && d.status !== "ok" && d.status !== "cached" && d.status !== "unlinked") {
      const n = h("div", { class: "d-note", role: "status", text: NOTES[d.status] || d.message || NOTES.error });
      if (d.status === "no_key") n.append(" ", button({ label: "Open Settings", kind: "ghost", onClick: () => state.goto && state.goto("settings") }));
      parts.push(n);
    } else if (d && d.status === "cached") parts.push(h("div", { class: "d-note", role: "status", text: `Showing saved details. ${NOTES[d.why] || "They may be out of date."}` }));
    if (data) {
      if (data.summary) parts.push(h("p", { class: "d-summary", text: data.summary }));
      if (data.description) {
        const det = h("details", { class: "d-fold" }, h("summary", { text: "Description" }), h("div", { class: "d-text", text: data.description }),
          button({ label: "Read more on Nexus", kind: "ghost", onClick: () => call("open_nexus_page", folder) }));
        det.open = descOpen;
        det.addEventListener("toggle", () => { descOpen = det.open; });
        parts.push(det);
      }
    }
    parts.push(versionsSection());
    return parts;
  }

  function fetchVersions() {
    const v = state.versions.get(folder);
    const done = v && (v.status === "ok" || v.status === "cached");
    if (!folder || done || versLoading.has(folder)) return;
    versLoading.add(folder);
    call("mod_versions", folder);
    render();
  }

  function versionsSection() {
    const v = state.versions.get(folder);
    const det = h("details", { class: "d-fold" }, h("summary", { text: "Versions" }));
    if (verOpen) {
      if (versLoading.has(folder) && !v) det.append(h("div", { class: "d-note", role: "status", text: "Loading changelog..." }));
      else if (v && v.versions.length) {
        for (const it of v.versions) det.append(h("div", { class: "d-ver" }, h("div", { class: "d-vname", text: it.version }), h("ul", {}, it.lines.map((l) => h("li", { text: l })))));
      } else if (v) det.append(h("div", { class: "d-note", role: "status", text: v.status === "ok" || v.status === "cached" ? "No changelog entries." : (NOTES[v.status] || v.message) }));
    }
    det.open = verOpen;
    det.addEventListener("toggle", () => {
      if (det.open === verOpen) return; // the toggle event of our own `open = verOpen` in a render
      verOpen = det.open;
      if (verOpen) fetchVersions();
    });
    return det;
  }

  function render() {
    const r = row();
    if (!folder || !r) { el.hidden = true; return; }
    el.hidden = false;
    title.textContent = folder;
    title.title = folder;
    body.replaceChildren(...(r.linked ? nexusSection(r) : [h("div", { class: "d-note", role: "status", text: "Not linked to Nexus. Link it to see the mod page details and detect updates." })]),
      h("div", { class: "d-folder muted", text: folder }), r.kind !== "ok" && r.hint ? h("p", { class: "d-hint muted", text: r.hint }) : null, actions(r));
  }

  function show(f) {
    folder = f;
    clearTimeout(timer);
    render();
    const r = row();
    if (r && r.linked) {
      loading.add(f);
      failed.delete(f);
      render();
      timer = setTimeout(async () => {
        const res = await call("mod_details", f);
        if (!res.ok) { loading.delete(f); failed.set(f, res.error); if (folder === f) render(); }
      }, 150);
    }
  }

  function hide() { folder = null; clearTimeout(timer); el.hidden = true; }

  const off = watch((what, p) => {
    if (what === "details") { loading.delete(p.folder); if (p.folder === folder) render(); }
    else if (what === "versions") { versLoading.delete(p.folder); if (p.folder === folder) render(); }
    else if (what === "row") { if (p.row.folder === folder) render(); }
    else if (what === "state" && folder) render();
  });

  return { el, show, hide, current: () => folder, destroy() { off(); clearTimeout(timer); } };
}
