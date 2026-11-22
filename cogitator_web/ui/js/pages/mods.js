// The Mods page: banners, toolbar, filter and the table. Rows are keyed by folder and patched in place; a full "state"
// push only reorders / adds / removes row elements, and a row whose data did not change is not touched.
import { call } from "../api.js";
import { showMenu } from "../components/menu.js";
import { createDetails } from "../components/details.js";
import { badge, button, setBadge, setSwitch, toggleSwitch } from "../components/widgets.js";
import { toast } from "../components/toast.js";
import { h } from "../dom.js";
import { icon } from "../icons.js";
import { state, watch } from "../store.js";

export const modsApi = { current: null };

const COLUMNS = ["On/Off", "Mod", "Version", "Status"]; // the list is always in load order, so the headers are plain labels
// status column: icon only (the text is the tooltip and a visually hidden label); other states keep a text badge
const STATUS_MARKS = { ok: ["ok", "check"], update: ["warn", "up"], unlinked: ["dim", "dash"], error: ["bad", "error"] };
const versionText = (r) => (r.kind === "unlinked" ? "-" : r.kind === "update" && r.latest && r.latest !== "-" ? `${r.installed} → ${r.latest}` : r.installed || "-");
const TONE = { ok: "ok", warn: "warn", bad: "bad", dim: "", "": "" };

const CHIPS = [["all", "All", () => true], ["enabled", "Enabled", (r) => r.en === "on"], ["updates", "Updates", (r) => r.kind === "update"],
  ["unlinked", "Not linked", (r) => r.kind === "unlinked"]];
const FILTER_MSG = "Clear the filter to change the load order";
let activeChip = "all"; // kept across list refreshes (and page switches)
let panelOpen = false; // the details panel: opened by a click (or ArrowRight on a row), follows the selection, closed by x / Esc / ArrowLeft
const selSet = new Set(); // every selected folder; state.selected is the cursor (the one the panel and Ctrl+Up/Down follow)
let anchor = null; // where a Shift+click range starts
const chipTest = (id) => (CHIPS.find((c) => c[0] === id) || CHIPS[0])[2];

export const title = "Mods";

export function mount(container, ctx) {
  const entries = new Map(); // folder -> { tr, sig, sw, name, installed, latest, status }
  let lastIndex = -1;

  const banner = h("div", { class: "banner", role: "status", hidden: true });
  const updBanner = h("div", { class: "banner", role: "status", hidden: true });
  // a banner with a text and an action button
  const mkBanner = (label, id) => {
    const text = h("span", { class: "grow" }), btn = button({ label, kind: "secondary", onClick: () => call(id) });
    const el = h("div", { class: "banner banner-row", role: "status", hidden: true }, text, btn);
    return { el, set(t) { el.hidden = !t; text.textContent = t || ""; } };
  };
  const modsBanner = mkBanner("Update all", "update_all"), fwBanner = mkBanner("Update", "framework_update");

  // ---- toolbar
  const check = button({ label: "Check updates", icon: "sync", kind: "secondary", title: "Check Nexus for updates (R, F5)", onClick: () => call("check_updates") });
  const importBtn = button({ label: "Import zip", icon: "add", kind: "secondary", title: "Import zip(s) (Ctrl+I)", onClick: () => call("import_zips") });
  const launchBtn = button({ label: "Launch game", icon: "play", kind: "primary", title: "Launch Darktide (G)", onClick: () => call("launch") });
  const globalBtn = button({ label: "Mods on", icon: "power", kind: "secondary", title: "Mods on or off: click to switch (T)", onClick: () => call("toggle_global") });
  const filter = h("input", { class: "input", type: "search", placeholder: "Filter mods", "aria-label": "Filter mods", autocomplete: "off", spellcheck: "false" });
  const search = h("div", { class: "search" }, icon("search"), filter);
  const toolbar = h("div", { class: "toolbar", role: "toolbar", "aria-label": "Mods" }, launchBtn, check, importBtn, globalBtn);
  const chipEls = new Map();
  const chipBar = h("div", { class: "chips", role: "group", "aria-label": "Show" }, CHIPS.map(([id, label]) => {
    const b = h("button", { type: "button", class: "fchip", "aria-pressed": String(id === activeChip) }, h("span", { text: label }), h("span", { class: "n" }));
    b.addEventListener("click", () => { activeChip = id; reconcile(null); });
    chipEls.set(id, { b, label, n: b.lastChild });
    return b;
  }), h("span", { class: "grow" }), search);
  const shown = () => { const t = chipTest(activeChip); return state.order.filter((f) => t(state.rows.get(f))); };
  let ftimer = null;
  filter.value = state.filter;
  filter.addEventListener("input", () => { clearTimeout(ftimer); ftimer = setTimeout(() => call("set_filter", filter.value), 120); });
  filter.addEventListener("keydown", (e) => { if (e.key === "Escape" && filter.value) { filter.value = ""; call("set_filter", ""); e.stopPropagation(); } else if (e.key === "ArrowDown") { e.preventDefault(); focusRow(state.selected || shown()[0]); } });

  // ---- table
  const headRow = h("tr", {}, COLUMNS.map((label) => h("th", { scope: "col", text: label })));
  const tbody = h("tbody", {});
  const table = h("table", { class: "mods", "aria-label": "Mods", "aria-multiselectable": "true" },
    h("colgroup", {}, h("col", { class: "c-en" }), h("col", { class: "c-mod" }), h("col", { class: "c-version" }), h("col", { class: "c-status" })),
    h("thead", {}, headRow), tbody);
  const empty = h("div", { class: "empty", hidden: true });
  const selCount = h("span", { class: "n", role: "status" });
  const enBtn = button({ label: "Enable", kind: "secondary", onClick: () => bulk("enable") });
  const disBtn = button({ label: "Disable", kind: "secondary", onClick: () => bulk("disable") });
  const updBtn = button({ label: "Update", kind: "secondary", onClick: () => bulk("update") });
  const rmBtn = button({ label: "Remove...", kind: "secondary", onClick: () => bulk("remove") });
  const clrBtn = button({ label: "Clear selection", icon: "close", iconOnly: true, kind: "ghost", title: "Clear selection (Esc)", onClick: () => { clearMulti(); focusRow(state.selected); } });
  const selBar = h("div", { class: "selbar", role: "toolbar", "aria-label": "Selected mods", hidden: true }, selCount, enBtn, disBtn, updBtn, rmBtn, h("span", { class: "grow" }), clrBtn);
  const marquee = h("div", { class: "marquee", "aria-hidden": "true", hidden: true });
  const wrap = h("div", { class: "table-wrap" }, table, empty, marquee, selBar);
  const panel = createDetails({ onClose: (refocus) => closePanel(refocus) });
  container.append(banner, updBanner, modsBanner.el, fwBanner.el, toolbar, chipBar, h("div", { class: "mods-layout" }, wrap, panel.el));
  function closePanel(refocus) {
    panelOpen = false;
    panel.hide();
    if (refocus && state.selected) focusRow(state.selected);
  }

  // ---- header
  // a problem banner: DML / DMF missing or incomplete (and the patch banner, e.g. mods are off); nothing is shown when all is well
  const fwProblems = (fw) => [["Mod Loader (DML)", fw.dml], ["Mod Framework (DMF)", fw.dmf]].filter(([, f]) => f && f.status !== "installed").map(([k, f]) => `${k} is ${f.status}.`);
  function renderHeader() {
    const hd = state.header;
    if (!hd) return;
    renderUpdates();
    const text = [hd.banner, ...fwProblems(hd.frameworks)].filter(Boolean).join(" ");
    banner.hidden = !text;
    banner.textContent = text;
    updBanner.hidden = !hd.update_banner;
    updBanner.textContent = hd.update_banner || "";
    globalBtn.lastChild.textContent = hd.mods === "ON" ? "Mods on" : "Mods off";
    check.classList.toggle("busy", !!hd.checking);
    check.disabled = !!hd.checking;
    check.lastChild.textContent = hd.checking ? "Checking..." : "Check updates";
  }

  function renderUpdates() {
    const n = [...state.rows.values()].filter((r) => r.kind === "update").length;
    modsBanner.set(n ? `${n} mod update${n === 1 ? "" : "s"} available.` : "");
    const hd = state.header, fw = hd ? hd.frameworks : null;
    const names = fw ? [["Mod Loader", fw.dml], ["Mod Framework", fw.dmf]].filter(([, f]) => f && f.update).map(([k, f]) => `${k} ${f.update}`) : [];
    fwBanner.set(names.length ? `Update available: ${names.join(", ")}.` : "");
  }

  // ---- rows
  function chips(row) {
    const out = [];
    if (row.pinned) out.push(h("span", { class: "pin-mark", title: `Version pinned: ${row.pinned}` }, icon("pin"), h("span", { class: "sr-only", text: `pinned ${row.pinned}` })));
    return out;
  }

  function createRow(row) {
    const sw = toggleSwitch({ label: row.folder, checked: false, tabindex: "-1", onChange: () => { if (!selSet.has(row.folder)) select(row.folder, false); call("toggle", row.folder); } });
    const name = h("td", { class: "name" });
    const version = h("td", { class: "ver" }), statusCell = h("td");
    const status = badge({ value: "" });
    statusCell.append(status);
    const tr = h("tr", { tabindex: "-1", "aria-selected": "false", data: { folder: row.folder } }, h("td", {}, h("div", { class: "en" }, h("span", { class: "grip", "aria-hidden": "true", title: "Drag to reorder" }, icon("grip")), sw)), name, version, statusCell);
    const e = { tr, sig: "", sw, name, version, status, grip: tr.querySelector(".grip") };
    tr.addEventListener("click", (ev) => {
      if (skipClick) return;
      if (ev.ctrlKey || ev.metaKey) toggleSel(row.folder);
      else if (ev.shiftKey) rangeSel(row.folder);
      else { panelOpen = true; select(row.folder, false); }
    });
    tr.addEventListener("dblclick", (ev) => { if (!ev.target.closest(".switch")) call("toggle", row.folder); });
    wireDrag(tr, row.folder, e.grip);
    tr.addEventListener("contextmenu", (ev) => {
      ev.preventDefault();
      if (selSet.has(row.folder) && picked().length > 1) { state.selected = row.folder; syncSelection(); openManyMenu(ev.clientX, ev.clientY); return; }
      select(row.folder, true);
      openMenu(row.folder, ev.clientX, ev.clientY);
    });
    return e;
  }

  // ---- drag a mod to a new place in the load order (a listed mod can be dropped before or after another listed mod)
  let dragging = null;
  const listed = (f) => { const r = state.rows.get(f); return !!r && r.present && r.en !== "unlisted"; };
  const clearDrop = () => { for (const { tr } of entries.values()) tr.classList.remove("drop-before", "drop-after", "dragging"); };
  function wireDrag(tr, folder, grip) {
    tr.addEventListener("dragstart", (ev) => {
      if (ev.target !== grip || !listed(folder) || !canReorder()) { ev.preventDefault(); return; }
      ev.dataTransfer.setDragImage(tr, 16, 16);
      dragging = folder;
      ev.dataTransfer.effectAllowed = "move";
      ev.dataTransfer.setData("text/plain", folder);
      tr.classList.add("dragging");
    });
    tr.addEventListener("dragend", () => { dragging = null; clearDrop(); });
    tr.addEventListener("dragover", (ev) => {
      if (!dragging || dragging === folder || !listed(folder)) return;
      ev.preventDefault();
      ev.dataTransfer.dropEffect = "move";
      const r = tr.getBoundingClientRect(), after = ev.clientY > r.top + r.height / 2;
      tr.classList.toggle("drop-after", after);
      tr.classList.toggle("drop-before", !after);
    });
    tr.addEventListener("dragleave", () => tr.classList.remove("drop-before", "drop-after"));
    tr.addEventListener("drop", (ev) => {
      if (!dragging || dragging === folder || !listed(folder)) return;
      ev.preventDefault();
      const r = tr.getBoundingClientRect(), after = ev.clientY > r.top + r.height / 2, f = dragging;
      dragging = null;
      clearDrop();
      select(f, false);
      call("move_to", f, folder, after);
    });
  }

  const canReorder = () => !state.filter && activeChip === "all"; // the list shows the whole load order
  function patchRow(e, row) {
    const sig = JSON.stringify(row) + canReorder();
    if (sig === e.sig) return;
    e.sig = sig;
    e.tr.classList.toggle("is-off", row.en !== "on");
    setSwitch(e.sw, row.en === "on", `${row.folder}: ${row.en === "on" ? "on" : row.en === "off" ? "off" : "not in load order"}`);
    e.sw.classList.toggle("is-unlisted", row.en === "unlisted");
    e.sw.disabled = !row.present;
    const can = row.present && row.en !== "unlisted" && canReorder();
    e.grip.draggable = can;
    e.grip.hidden = !canReorder(); // only the grip starts a drag; the rest of the row is for selecting
    e.tr.classList.toggle("can-drag", can);
    e.name.replaceChildren(h("div", { class: "in" }, h("span", { class: "t", text: row.folder, title: row.folder }), ...chips(row), row.suffix ? h("span", { class: "muted", text: row.suffix }) : null));
    e.version.textContent = versionText(row);
    e.version.classList.toggle("dash", row.kind === "unlinked");
    const mark = STATUS_MARKS[row.kind];
    if (mark) {
      e.status.className = `st-mark ${mark[0]}`;
      e.status.title = row.status;
      e.status.replaceChildren(icon(mark[1]), h("span", { class: "sr-only", text: row.status }));
    } else {
      e.status.title = "";
      setBadge(e.status, { tone: TONE[row.tone] ?? "", value: row.status });
    }
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
    for (const f of [...selSet]) if (!seen.has(f)) selSet.delete(f);
    if (anchor && !seen.has(anchor)) anchor = null;
    if (keep && seen.has(keep)) { state.selected = keep; selSet.clear(); selSet.add(keep); anchor = keep; }
    else if (state.selected && !seen.has(state.selected)) {
      state.selected = [...selSet].pop() || state.order[Math.min(Math.max(lastIndex, 0), state.order.length - 1)] || null;
      if (state.selected && !selSet.size) selSet.add(state.selected);
    } else if (state.selected && !selSet.size) selSet.add(state.selected);
    syncSelection();
    renderUpdates();
    if (keep && entries.has(keep)) entries.get(keep).tr.scrollIntoView({ block: "nearest" });
    const vis = shown(), visSet = new Set(vis);
    for (const [f, e] of entries) e.tr.hidden = !visSet.has(f);
    renderChips();
    empty.hidden = vis.length > 0 || !state.loaded;
    empty.textContent = state.order.length ? "No mods match this filter. Choose All to see every mod."
      : state.filter ? `No mods match "${state.filter}". Press Esc in the filter box to clear it.` : state.emptyHint;
    wrap.classList.toggle("is-empty", !empty.hidden);
    if (document.activeElement !== filter) filter.value = state.filter;
    ctx.setSummary(state.summary);
  }

  function renderChips() {
    for (const [id, c] of chipEls) {
      const t = chipTest(id);
      const n = id === "all" ? null : [...state.rows.values()].filter(t).length;
      c.n.textContent = n === null ? "" : String(n);
      c.b.setAttribute("aria-pressed", String(id === activeChip));
      c.b.setAttribute("aria-label", n === null ? c.label : `${c.label}, ${n}`);
    }
  }

  // ---- selection
  const picked = () => shown().filter((f) => selSet.has(f)); // selected AND visible, in list order
  function syncSelection() {
    let tab = state.selected && entries.has(state.selected) ? state.selected : shown()[0];
    for (const [f, e] of entries) {
      e.tr.setAttribute("aria-selected", String(selSet.has(f)));
      e.tr.tabIndex = f === tab ? 0 : -1;
    }
    ctx.setHint(state.order.length ? "" : state.emptyHint); // only the empty-state text; the details panel carries the per-mod hint
    if (panelOpen && state.selected !== panel.current()) { if (state.selected && state.rows.has(state.selected)) panel.show(state.selected); else panel.hide(); }
    lastIndex = state.selected ? state.order.indexOf(state.selected) : lastIndex;
    renderBar();
  }

  function renderBar() {
    const sel = picked();
    selBar.hidden = sel.length < 2;
    if (selBar.hidden) return;
    selCount.textContent = `${sel.length} selected`;
    updBtn.hidden = !sel.some((f) => (state.rows.get(f) || {}).kind === "update");
  }

  function select(folder, focus) {
    if (!entries.has(folder)) return;
    state.selected = folder;
    selSet.clear();
    selSet.add(folder);
    anchor = folder;
    syncSelection();
    if (focus) focusRow(folder);
  }

  function toggleSel(folder) {
    if (!entries.has(folder)) return;
    if (selSet.has(folder)) { selSet.delete(folder); if (state.selected === folder) state.selected = [...selSet].pop() || null; }
    else { selSet.add(folder); state.selected = folder; }
    anchor = folder;
    syncSelection();
  }

  function rangeSel(folder, focus) {
    const list = shown(), a = list.indexOf(anchor && entries.has(anchor) ? anchor : state.selected), b = list.indexOf(folder);
    if (b < 0) return;
    if (a < 0) { select(folder, focus); return; }
    selSet.clear();
    for (const f of list.slice(Math.min(a, b), Math.max(a, b) + 1)) selSet.add(f);
    state.selected = folder;
    syncSelection();
    if (focus) focusRow(folder);
  }

  function selectAll() {
    const list = shown();
    if (!list.length) return;
    selSet.clear();
    for (const f of list) selSet.add(f);
    if (!state.selected || !selSet.has(state.selected)) state.selected = list[0];
    if (!anchor || !selSet.has(anchor)) anchor = list[0];
    syncSelection();
  }

  function clearMulti() {
    selSet.clear();
    if (state.selected) selSet.add(state.selected);
    anchor = state.selected;
    syncSelection();
  }

  // ---- marquee: press on a row body and drag to box-select; Ctrl adds to the selection. Auto-scrolls near the top/bottom edge.
  let skipClick = false;
  const scroller = () => wrap.closest(".content");
  tbody.addEventListener("mousedown", (ev) => {
    if (ev.button !== 0 || ev.target.closest(".switch, .grip, button, a, input")) return;
    const sc = scroller();
    if (!sc || !ev.target.closest("tr")) return;
    const base = (ev.ctrlKey || ev.metaKey) ? new Set(selSet) : new Set();
    const start = { x: ev.clientX, y: ev.clientY + sc.scrollTop }; // y in scroll space, so the box stays put while the page scrolls
    let cur = { x: ev.clientX, y: ev.clientY }, active = false, raf = 0;
    const frame = () => {
      raf = 0;
      const sr = sc.getBoundingClientRect(), edge = 36;
      const near = cur.y < sr.top + edge || cur.y > sr.bottom - edge;
      if (cur.y < sr.top + edge) sc.scrollTop -= Math.ceil((sr.top + edge - cur.y) / 3);
      else if (cur.y > sr.bottom - edge) sc.scrollTop += Math.ceil((cur.y - (sr.bottom - edge)) / 3);
      const x1 = Math.min(start.x, cur.x), x2 = Math.max(start.x, cur.x);
      const y1 = Math.min(start.y, cur.y + sc.scrollTop) - sc.scrollTop, y2 = Math.max(start.y, cur.y + sc.scrollTop) - sc.scrollTop;
      const wr = wrap.getBoundingClientRect();
      marquee.hidden = false;
      marquee.style.left = `${x1 - wr.left}px`;
      marquee.style.top = `${y1 - wr.top}px`;
      marquee.style.width = `${x2 - x1}px`;
      marquee.style.height = `${y2 - y1}px`;
      const hit = [];
      for (const f of shown()) { const r = entries.get(f).tr.getBoundingClientRect(); if (r.bottom >= y1 && r.top <= y2 && r.right >= x1 && r.left <= x2) hit.push(f); }
      selSet.clear();
      for (const f of base) selSet.add(f);
      for (const f of hit) selSet.add(f);
      if (hit.length) { state.selected = hit[hit.length - 1]; anchor = hit[0]; }
      else if (!selSet.has(state.selected)) state.selected = [...selSet].pop() || null;
      syncSelection();
      if (near) raf = requestAnimationFrame(frame);
    };
    const move = (e) => {
      cur = { x: e.clientX, y: e.clientY };
      if (!active) {
        if (Math.hypot(cur.x - ev.clientX, cur.y - ev.clientY) < 4) return;
        active = true;
      }
      if (!raf) raf = requestAnimationFrame(frame);
    };
    const up = () => {
      document.removeEventListener("mousemove", move, true);
      document.removeEventListener("mouseup", up, true);
      if (raf) cancelAnimationFrame(raf);
      marquee.hidden = true;
      if (active) { skipClick = true; setTimeout(() => { skipClick = false; }, 0); }
    };
    document.addEventListener("mousemove", move, true);
    document.addEventListener("mouseup", up, true);
  });

  function focusRow(folder) {
    const e = entries.get(folder);
    if (e) { e.tr.focus(); e.tr.scrollIntoView({ block: "nearest" }); }
  }


  // ---- actions on the selected mod (also used by keyboard shortcuts)
  async function openMenu(folder, x, y) {
    const r = await call("row_menu", folder);
    if (r.ok && r.items.length) showMenu(x, y, r.items, (id) => call("row_action", folder, id));
  }

  // ---- actions on the whole selection (the bar and the right-click menu)
  function bulk(action) { const f = picked(); if (f.length > 1) call("bulk", action, f); }
  function openManyMenu(x, y) {
    const any = picked().some((f) => (state.rows.get(f) || {}).kind === "update");
    showMenu(x, y, [{ label: "Enable", id: "enable", enabled: true }, { label: "Disable", id: "disable", enabled: true },
      { label: "Update", id: "update", enabled: any }, null, { label: "Remove...", id: "remove", enabled: true }], (id) => bulk(id));
  }

  const api = {
    selected: () => state.selected,
    toggle() { if (state.selected) call("toggle", state.selected); },
    move(d) { if (!canReorder()) { toast(FILTER_MSG, "warn"); return; } if (state.selected && picked().length <= 1) call("move", state.selected, d); },
    selectAll,
    focusFilter() { filter.focus(); filter.select(); },
    inTable() { return tbody.contains(document.activeElement); },
    menuForSelected() {
      const e = entries.get(state.selected);
      if (!e) return;
      const r = e.tr.getBoundingClientRect();
      if (picked().length > 1) openManyMenu(r.left + 80, r.bottom);
      else openMenu(state.selected, r.left + 80, r.bottom);
    },
  };

  tbody.addEventListener("keydown", (e) => {
    const f = e.target.closest ? e.target.closest("tr") : null;
    if (!f || e.target.closest(".switch") && e.key !== " " && e.key !== "Enter") return;
    const list = shown(), i = list.indexOf(f.dataset.folder);
    if (e.key === "Escape" && picked().length > 1) { e.stopPropagation(); clearMulti(); return; }
    if (e.key === "Escape" && panelOpen) { e.stopPropagation(); closePanel(false); return; }
    if (e.key === "ArrowRight" && !panelOpen) { e.preventDefault(); panelOpen = true; select(f.dataset.folder, false); return; }
    if (e.key === "ArrowLeft" && panelOpen) { e.preventDefault(); closePanel(false); return; }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const n = list[Math.max(0, Math.min(list.length - 1, i + (e.key === "ArrowDown" ? 1 : -1)))];
      if (e.ctrlKey) api.move(e.key === "ArrowDown" ? 1 : -1);
      else if (e.shiftKey) rangeSel(n, true);
      else select(n, true);
    } else if (e.key === "Home" || e.key === "End") { e.preventDefault(); select(list[e.key === "Home" ? 0 : list.length - 1], true); }
    else if (e.key === " " || e.key === "Enter") { e.preventDefault(); if (!selSet.has(f.dataset.folder)) select(f.dataset.folder, false); call("toggle", f.dataset.folder); }
    else if (e.key === "ContextMenu" || (e.key === "F10" && e.shiftKey)) { e.preventDefault(); api.menuForSelected(); }
  });

  const off = watch((what, p) => {
    if (what === "state") reconcile(p.keep);
    else if (what === "row") {
      const e = entries.get(p.row.folder);
      if (e) { patchRow(e, state.rows.get(p.row.folder)); e.tr.hidden = !chipTest(activeChip)(state.rows.get(p.row.folder)); renderChips(); renderUpdates(); if (state.selected === p.row.folder) syncSelection(); ctx.setSummary(state.summary); }
    } else if (what === "header") renderHeader();
  });

  renderHeader();
  if (state.loaded) reconcile(null);
  else ctx.setHint("");
  modsApi.current = api;
  return { unmount() { off(); panel.destroy(); modsApi.current = null; }, focus() { if (shown().length) focusRow(state.selected || shown()[0]); else filter.focus(); } };
}
