// Boot, page router and keyboard shortcuts. Python pushes into window.cogitatorPush (store.js); this file wires the shell.
import { call } from "./api.js";
import { menuOpen } from "./components/menu.js";
import { ask, info, initModals, modalOpen, promptText } from "./components/modal.js";
import { buildDrawer, buildSidebar, buildStatusline, pageHead } from "./components/shell.js";
import { applyBackground } from "./background.js";
import { initToasts, toast } from "./components/toast.js";
import { h } from "./dom.js";
import { detectIcons } from "./icons.js";
import * as modsPage from "./pages/mods.js";
import { modsApi } from "./pages/mods.js";
import * as toolsPage from "./pages/tools.js";
import * as profilesPage from "./pages/profiles.js";
import * as snapshotsPage from "./pages/snapshots.js";
import * as settingsPage from "./pages/settings.js";
import { applyTheme, on, state } from "./store.js";

const PAGES = { mods: modsPage, profiles: profilesPage, snapshots: snapshotsPage, tools: toolsPage, settings: settingsPage };
let sidebar, content, head, status, drawer, mounted = null;

function go(id, focus = false) {
  if (!PAGES[id]) return;
  if (mounted) mounted.unmount();
  const page = PAGES[id];
  state.page = id;
  content.replaceChildren();
  head.replaceWith((head = pageHead(page.title, page.subtitle)));
  const host = h("div", { class: "page" });
  content.append(host);
  mounted = page.mount(host, status);
  sidebar.select(id);
  if (focus && mounted.focus) mounted.focus();
}

async function searchNexus() {
  const text = await promptText({ title: "Search Nexus Mods", hint: "part of a mod's name" });
  if (text) await call("search_nexus", text);
}

async function showHelp() {
  const r = await call("help");
  if (r.ok) info({ title: r.title, body: r.body });
}

function onKey(e) {
  if (modalOpen() || menuOpen()) return;
  const ctrl = e.ctrlKey || e.metaKey;
  const k = e.key;
  const typing = e.target.closest && e.target.closest("input, textarea, select");
  // the browser's own reload / print / find must never act on this window
  if (k === "F5" || (ctrl && (k === "r" || k === "R" || k === "p" || k === "P"))) e.preventDefault();
  const mods = modsApi.current;
  const run = (fn) => { e.preventDefault(); fn(); };
  if (k === "F1") return run(showHelp);
  if (k === "F5") return run(() => call("check_updates"));
  if (k === "F3") return run(searchNexus);
  if (ctrl && !e.shiftKey && !e.altKey) {
    const map = { i: () => call("import_zips"), g: () => call("launch"), d: () => call("doctor"), t: () => call("toggle_global"), u: () => call("update_all"), q: () => call("quit"),
      k: () => { go("settings", true); }, f: () => { if (state.page !== "mods") go("mods"); if (modsApi.current) modsApi.current.focusFilter(); } };
    const fn = map[k.toLowerCase()];
    if (fn) return run(fn);
    return;
  }
  if (ctrl || e.altKey || typing || !mods || !mods.inTable()) return;
  // plain letters act only while the table has focus (typing in a field must not trigger anything)
  const letters = { r: () => call("check_updates"), i: () => call("import_zips"), g: () => call("launch"), d: () => call("doctor"), t: () => call("toggle_global"),
    m: () => call("link_all"), f: () => call("framework_update"),
    k: () => go("settings", true), x: () => mods.mark(), b: () => mods.bulk(), n: () => mods.note(), o: () => mods.cycleSort(), "*": () => mods.fav(), "/": () => mods.focusFilter() };
  const fn = letters[k.length === 1 ? k.toLowerCase() : k];
  if (fn) run(fn);
}

async function boot() {
  detectIcons();
  const r = await call("boot");
  const settings = r.ok ? r.settings : { gui_theme: "system", version: "" };
  applyTheme(settings.gui_theme);
  await applyBackground(settings.gui_background);
  const app = document.getElementById("app");
  initModals(document.getElementById("modal-root"), app);
  state.goto = (id) => go(id, true);
  sidebar = buildSidebar((id) => go(id, true), settings.version);
  head = pageHead("", "");
  content = h("main", { class: "content", id: "content" });
  drawer = buildDrawer();
  status = buildStatusline();
  app.append(sidebar.el, h("div", { class: "main" }, head, content, drawer.el, status.el));
  document.addEventListener("keydown", onKey);
  document.addEventListener("contextmenu", (e) => { if (!(e.target.closest && e.target.closest("input, textarea"))) e.preventDefault(); });
  initToasts(document.getElementById("modal-root"));
  on("log", (p) => {
    const st = String(p.style || "");
    if (/red/.test(st)) { toast(p.text, "bad"); drawer.flagError(); }
    else if (/(green|yellow|bold)/.test(st)) toast(p.text, /yellow/.test(st) ? "warn" : "ok");
  });
  on("ask", (p) => ask(p));
  on("report", (p) => info({ title: p.title, segments: p.segments, wide: true }));
  go("mods");
  await call("ready");
}

boot();
