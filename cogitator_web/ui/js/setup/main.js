// The setup window: renders each pushed `setup_state` (page, footer, step indicator) and sends the user's actions to Python.
import { call } from "../api.js";
import { h, clear } from "../dom.js";
import { detectIcons } from "../icons.js";
import { applyTheme, on } from "../store.js";
import { info, initModals, ask, modalOpen } from "../components/modal.js";
import { button } from "../components/widgets.js";
import { PAGES, bindRender } from "./pages.js";

const LABELS = { welcome: "Welcome", game: "Game", key: "Key", components: "Components", review: "Review", progress: "Install", finish: "Done" };
const draft = { game: "", key: "", show: false };
const details = { open: false };
let S = null, root = null, lastPath = null;

async function copy() {
  const r = await call("copy_details");
  const text = r.ok ? r.text : "";
  if (!text) return;
  try { await navigator.clipboard.writeText(text); return; } catch (e) { /* no clipboard access: show it instead */ }
  info({ title: "Details", body: text, wide: true });
}

function footer(s) {
  const n = s.nav;
  if (s.page === "express" || !n.visible) return null;
  if (s.page === "finish") {
    const label = s.finish.launch ? "Start Cogitator" : "Finish";
    const fin = button({ label, kind: "primary", onClick: () => call("finish") });
    fin.dataset.fk = "finish";
    return h("div", { class: "foot" }, h("span", { class: "reason" }), button({ label: "Close", kind: "ghost", onClick: () => call("cancel") }), fin);
  }
  const cancel = button({ label: n.cancel.label, kind: "ghost", disabled: !n.cancel.enabled, onClick: () => call("cancel") });
  const back = button({ label: n.back.label, kind: "secondary", disabled: !n.back.enabled, onClick: () => call("back") });
  const next = button({ label: n.next.label, kind: "primary", disabled: !n.next.enabled, onClick: () => call("next") });
  cancel.dataset.fk = "cancel"; back.dataset.fk = "back"; next.dataset.fk = "next";
  const parts = s.page === "progress" ? [cancel] : [cancel, back, next];
  return h("div", { class: "foot" }, h("span", { class: "reason", role: "status", text: s.page === "progress" ? "" : n.reason }), ...parts);
}

function steps(s) {
  const i = s.steps.indexOf(s.page);
  return h("nav", { class: "steps", "aria-label": "Steps" }, s.steps.map((name, k) =>
    h("span", { class: `step${k < i ? " past" : ""}`, "aria-current": k === i ? "step" : null, text: LABELS[name] || name })));
}

function render() {
  if (!S) return;
  const s = S;
  if (s.game.path !== lastPath) { draft.game = s.game.path || draft.game; lastPath = s.game.path; }
  if (s.key.ok || s.key.skipped) { draft.key = ""; draft.show = false; }
  const active = document.activeElement;
  const fk = active && active.dataset ? active.dataset.fk : null;
  const sel = active && "selectionStart" in active ? [active.selectionStart, active.selectionEnd] : null;
  const card = h("main", { class: "card", id: "card" }, PAGES[s.page](s, { draft, call, details, copy }));
  clear(root).append(steps(s), card, footer(s) || h("div", { class: "foot" }));
  if (fk) {
    const el = root.querySelector(`[data-fk="${CSS.escape(fk)}"]`);
    if (el && !el.disabled) {
      el.focus();
      if (sel && "setSelectionRange" in el) { try { el.setSelectionRange(sel[0], sel[1]); } catch (e) { /* not a text input */ } }
    }
  } else if (!modalOpen() && (!active || active === document.body)) {
    const first = card.querySelector("input, button:not(:disabled)");
    if (first) first.focus();
  }
}

function onKey(e) {
  if (modalOpen() || !S) return;
  if (e.key === "Escape") { e.preventDefault(); call("escape"); return; }
  if (e.key === "F5" || (e.ctrlKey && "rRpP".includes(e.key))) { e.preventDefault(); return; }
  if (e.key === "Enter" && !e.ctrlKey && !e.altKey && !e.shiftKey) {
    const t = e.target;
    if (t && t.closest && t.closest("button, summary, a, textarea")) return; // Enter activates the focused control
    const wizard = ["welcome", "game", "key", "components"].includes(S.page);
    if (wizard && S.nav.visible && S.nav.next.enabled) { e.preventDefault(); call("next"); }
  }
}

async function boot() {
  detectIcons();
  applyTheme("system");
  root = document.getElementById("setup");
  initModals(document.getElementById("modal-root"), root);
  bindRender(render);
  on("ask", (p) => ask(p));
  on("setup_state", (p) => { S = p; render(); });
  document.addEventListener("keydown", onKey);
  document.addEventListener("contextmenu", (e) => { if (!(e.target.closest && e.target.closest("input, textarea"))) e.preventDefault(); });
  const r = await call("boot");
  if (r.ok) { S = r.state; render(); }
  await call("ready");
}

boot();
