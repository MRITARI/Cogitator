// The Mods page: header badges, toolbar, filter and the table. Rows are keyed by folder and patched in place; a full "state"
// push only reorders / adds / removes row elements, and a row whose data did not change is not touched.
import { call } from "../api.js";
import { showMenu } from "../components/menu.js";
import { badge, button, setBadge, setSwitch, toggleSwitch } from "../components/widgets.js";
import { h } from "../dom.js";
import { icon } from "../icons.js";
import { state, watch } from "../store.js";

export const modsApi = { current: null };

const COLUMNS = [["en", "On/Off"], ["mod", "Mod"], ["installed", "Installed"], ["latest", "Latest"], ["status", "Status"]];
const TONE = { ok: "ok", warn: "warn", bad: "bad", dim: "", "": "" };

export const title = "Mods";
export const subtitle = "Turn mods on or off, change their load order and check Nexus for updates.";

export function mount(container, ctx) {
  const entries = new Map(); // folder -> { tr, sig, sw, name, installed, latest, status }
  let lastIndex = -1;

  // ---- header badges
  const bDml = badge({ key: "DML", value: "" }), bDmf = badge({ key: "DMF", value: "" }), bMods = badge({ key: "Mods", value: "" }), bNexus = badge({ key: "Nexus", value: "" });
  const badges = h("div", { class: "badges", role: "group", "aria-label": "Status" }, bDml, bDmf, bMods, bNexus);
  const banner = h("div", { class: "banner", role: "status", hidden: true });

  // ---- toolbar
  const check = button({ label: "Check updates", icon: "sync", kind: "primary", title: "Check Nexus for updates (R, F5)", onClick: () => call("check_updates") });
  const importBtn = button({ label: "Import zip", icon: "add", title: "Import zip(s) (Ctrl+I)", onClick: () => call("import_zips") });
  const launchBtn = button({ label: "Launch game", icon: "play", title: "Launch Darktide (G)", onClick: () => call("launch") });
  const globalBtn = button({ label: "Mods on/off", icon: "power", title: "Turn all mods on or off (T)", onClick: () => call("toggle_global") });
  const upBtn = button({ label: "Move up", icon: "up", iconOnly: true, kind: "secondary", title: "Move up (Ctrl+Up)", onClick: () => api.move(-1), disabled: true });
  const downBtn = button({ label: "Move down", icon: "down", iconOnly: true, kind: "secondary", title: "Move down (Ctrl+Down)", onClick: () => api.move(1), disabled: true });
  const filter = h("input", { class: "input", type: "search", placeholder: "Filter mods", "aria-label": "Filter mods", autocomplete: "off", spellcheck: "false" });
  const search = h("div", { class: "search" }, icon("search"), filter);
  const toolbar = h("div", { class: "toolbar", role: "toolbar", "aria-label": "Mods" }, check, importBtn, launchBtn, globalBtn, upBtn, downBtn, h("span", { class: "grow" }), search);
  let ftimer = null;
  filter.value = state.filter;
  filter.addEventListener("input", () => { clearTimeout(ftimer); ftimer = setTimeout(() => call("set_filter", filter.value), 120); });
  filter.addEventListener("keydown", (e) => { if (e.key === "Escape" && filter.value) { filter.value = ""; call("set_filter", ""); e.stopPropagation(); } else if (e.key === "ArrowDown") { e.preventDefault(); focusRow(state.selected || state.order[0]); } });

  // ---- table
  const heads = new Map();
  const headRow = h("tr", {}, COLUMNS.map(([id, label]) => {
    const arrow = h("span", { class: "sortmark", "aria-hidden": "true" });
    const b = h("button", { type: "button", title: `Sort by ${label}` }, h("span", { text: label }), arrow);
    b.addEventListener("click", () => call("set_sort", id));
    const th = h("th", { scope: "col", "aria-sort": "none" }, b);
    heads.set(id, { th, arrow });
    return th;
  }));
  const tbody = h("tbody", {});
  const table = h("table", { class: "mods", "aria-label": "Mods" },
    h("colgroup", {}, h("col", { class: "c-en" }), h("col", { class: "c-mod" }), h("col", { class: "c-installed" }), h("col", { class: "c-latest" }), h("col", { class: "c-status" })),
    h("thead", {}, headRow), tbody);
  const empty = h("div", { class: "empty", hidden: true });
  const wrap = h("div", { class: "table-wrap" }, table, empty);
  container.append(badges, banner, toolbar, wrap);

  // ---- header
  const fwBadge = (el, key, f) => {
    if (!f) return;
    const installed = f.status === "installed";
    setBadge(el, { tone: !installed ? "bad" : (f.update ? "warn" : "ok"), key, value: installed ? `installed${f.version ? " " + f.version : ""}${f.update ? `, update ${f.update}` : ""}` : f.status });
  };
  function renderHeader() {
    const hd = state.header;
    if (!hd) return;
    fwBadge(bDml, "DML", hd.frameworks.dml);
    fwBadge(bDmf, "DMF", hd.frameworks.dmf);
    setBadge(bMods, { tone: hd.mods === "ON" ? "ok" : (hd.mods === "OFF" ? "bad" : "warn"), key: "Mods", value: hd.mods === "ON" ? "on" : (hd.mods === "OFF" ? "off" : String(hd.mods).toLowerCase()) });
    const nx = hd.nexus;
    if (!nx.key_set) setBadge(bNexus, { tone: "warn", key: "Nexus", value: "no API key" });
    else setBadge(bNexus, { tone: nx.premium === null ? "" : "ok", key: "Nexus", value: nx.premium === null ? "key set" : (nx.premium ? "premium" : "free") + (nx.quota ? `, quota ${nx.quota}` : "") });
    banner.hidden = !hd.banner;
    banner.textContent = hd.banner || "";
    globalBtn.lastChild.textContent = hd.mods === "ON" ? "Turn mods off" : "Turn mods on";
    check.classList.toggle("busy", !!hd.checking);
    check.disabled = !!hd.checking;
    check.lastChild.textContent = hd.checking ? "Checking..." : "Check updates";
    bDml.title = hd.game_dir;
  }

  // ---- rows
  function chips(row) {
    const out = [];
    if (row.fav) out.push(h("span", { class: "chip on", title: "Favourite" }, icon("star"), "Favourite"));
    if (row.pinned) out.push(h("span", { class: "chip", title: "Version pinned" }, icon("pin"), `pinned ${row.pinned}`));
    if (row.note) out.push(h("span", { class: "chip", title: "Has a note" }, icon("note"), "note"));
    for (const t of row.tags.slice(0, 3)) out.push(h("span", { class: "chip", text: `#${t}` }));
    if (row.marked) out.push(h("span", { class: "chip on", title: "Marked for bulk actions" }, icon("mark"), "marked"));
    return out;
  }

  function createRow(row) {
    const sw = toggleSwitch({ label: row.folder, checked: false, tabindex: "-1", onChange: () => { select(row.folder, false); call("toggle", row.folder); } });
    const name = h("td", { class: "name" });
    const installed = h("td"), latest = h("td"), statusCell = h("td");
    const status = badge({ value: "" });
    statusCell.append(status);
    const tr = h("tr", { tabindex: "-1", "aria-selected": "false", data: { folder: row.folder } }, h("td", {}, sw), name, installed, latest, statusCell);
    const e = { tr, sig: "", sw, name, installed, latest, status };
    tr.addEventListener("click", () => select(row.folder, false));
    tr.addEventListener("dblclick", (ev) => { if (!ev.target.closest(".switch")) call("toggle", row.folder); });
    tr.addEventListener("contextmenu", (ev) => { ev.preventDefault(); select(row.folder, true); openMenu(row.folder, ev.clientX, ev.clientY); });
    return e;
  }

  function patchRow(e, row) {
    const sig = JSON.stringify(row);
    if (sig === e.sig) return;
    e.sig = sig;
    e.tr.classList.toggle("is-off", row.en !== "on");
    setSwitch(e.sw, row.en === "on", `${row.folder}: ${row.en === "on" ? "on" : row.en === "off" ? "off" : "not in load order"}`);
    e.sw.classList.toggle("is-unlisted", row.en === "unlisted");
    e.sw.disabled = !row.present;
    e.name.replaceChildren(h("div", { class: "in" }, h("span", { class: "t", text: row.folder, title: row.folder }), ...chips(row), row.suffix ? h("span", { class: "muted", text: row.suffix }) : null));
    e.installed.textContent = row.installed;
    e.latest.textContent = row.latest;
    e.installed.className = row.installed === "-" ? "muted" : "";
    e.latest.className = row.latest === "-" ? "muted" : "";
    setBadge(e.status, { tone: TONE[row.tone] ?? "", value: row.status });
  }

  function reconcile(keep) {
    const seen = new Set(state.order);
    for (const [f, e] of entries) if (!seen.has(f)) { e.tr.remove(); entries.delete(f); }
    state.order.forEach((f, i) => {
      const row = state.rows.get(f);
      let e = entries.get(f);
      if (!e) { e = createRow(row); entries.set(f, e); }
      patchRow(e, row);
      if (tbody.children[i] !== e.tr) tbody.insertBefore(e.tr, tbody.children[i] || null);
    });
    if (keep && seen.has(keep)) state.selected = keep;
    else if (state.selected && !seen.has(state.selected)) state.selected = state.order[Math.min(Math.max(lastIndex, 0), state.order.length - 1)] || null;
    syncSelection();
    if (keep && entries.has(keep)) entries.get(keep).tr.scrollIntoView({ block: "nearest" });
    empty.hidden = state.order.length > 0 || !state.loaded;
    empty.textContent = state.filter ? "No mods match the filter." : state.emptyHint;
    for (const [id, { th, arrow }] of heads) {
      const on = state.sort.col === id;
      th.setAttribute("aria-sort", on ? (state.sort.desc ? "descending" : "ascending") : "none");
      arrow.replaceChildren(on ? icon(state.sort.desc ? "down" : "up") : "");
    }
    if (document.activeElement !== filter) filter.value = state.filter;
    ctx.setSummary(state.summary);
    refreshToolbar();
  }

  // ---- selection
  function syncSelection() {
    let tab = state.selected && entries.has(state.selected) ? state.selected : state.order[0];
    for (const [f, e] of entries) {
      const sel = f === state.selected;
      e.tr.setAttribute("aria-selected", String(sel));
      e.tr.tabIndex = f === tab ? 0 : -1;
    }
    const row = state.rows.get(state.selected);
    ctx.setHint(row ? row.hint : state.emptyHint);
    lastIndex = state.selected ? state.order.indexOf(state.selected) : lastIndex;
  }

  function select(folder, focus) {
    if (!entries.has(folder)) return;
    state.selected = folder;
    syncSelection();
    refreshToolbar();
    if (focus) focusRow(folder);
  }

  function focusRow(folder) {
    const e = entries.get(folder);
    if (e) { e.tr.focus(); e.tr.scrollIntoView({ block: "nearest" }); }
  }

  function refreshToolbar() {
    const row = state.rows.get(state.selected);
    const can = !!row && row.present && row.en !== "unlisted";
    upBtn.disabled = downBtn.disabled = !can;
  }

  // ---- actions on the selected mod (also used by keyboard shortcuts)
  async function openMenu(folder, x, y) {
    const r = await call("row_menu", folder);
    if (r.ok && r.items.length) showMenu(x, y, r.items, (id) => call("row_action", folder, id));
  }

  const api = {
    selected: () => state.selected,
    toggle() { if (state.selected) call("toggle", state.selected); },
    move(d) { if (state.selected) call("move", state.selected, d); },
    mark() { if (state.selected) call("mark", state.selected); },
    fav() { if (state.selected) call("fav", state.selected); },
    note() { if (state.selected) call("edit_note", state.selected); },
    cycleSort() { call("cycle_sort"); },
    focusFilter() { filter.focus(); filter.select(); },
    inTable() { return tbody.contains(document.activeElement); },
    menuForSelected() {
      const e = entries.get(state.selected);
      if (e) { const r = e.tr.getBoundingClientRect(); openMenu(state.selected, r.left + 80, r.bottom); }
    },
  };

  tbody.addEventListener("keydown", (e) => {
    const f = e.target.closest ? e.target.closest("tr") : null;
    if (!f || e.target.closest(".switch") && e.key !== " " && e.key !== "Enter") return;
    const i = state.order.indexOf(f.dataset.folder);
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (e.ctrlKey) api.move(e.key === "ArrowDown" ? 1 : -1);
      else { const n = state.order[Math.max(0, Math.min(state.order.length - 1, i + (e.key === "ArrowDown" ? 1 : -1)))]; select(n, true); }
    } else if (e.key === "Home" || e.key === "End") { e.preventDefault(); select(state.order[e.key === "Home" ? 0 : state.order.length - 1], true); }
    else if (e.key === " " || e.key === "Enter") { e.preventDefault(); select(f.dataset.folder, false); call("toggle", f.dataset.folder); }
    else if (e.key === "ContextMenu" || (e.key === "F10" && e.shiftKey)) { e.preventDefault(); api.menuForSelected(); }
  });

  const off = watch((what, p) => {
    if (what === "state") reconcile(p.keep);
    else if (what === "row") {
      const e = entries.get(p.row.folder);
      if (e) { patchRow(e, state.rows.get(p.row.folder)); if (state.selected === p.row.folder) syncSelection(); ctx.setSummary(state.summary); refreshToolbar(); }
    } else if (what === "header") renderHeader();
  });

  renderHeader();
  if (state.loaded) reconcile(null);
  else ctx.setHint("");
  modsApi.current = api;
  return { unmount() { off(); modsApi.current = null; }, focus() { if (state.order.length) focusRow(state.selected || state.order[0]); else filter.focus(); } };
}
