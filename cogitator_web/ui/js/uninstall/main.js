// The uninstall / maintenance window: renders each pushed `uninstall_state` and sends the user's actions to Python.
import { call } from "../api.js";
import { h, clear } from "../dom.js";
import { detectIcons } from "../icons.js";
import { on } from "../store.js";
import { info, initModals, ask, modalOpen } from "../components/modal.js";
import { button } from "../components/widgets.js";
import { PAGES } from "./pages.js";

const LABELS = { choose: "Remove", review: "Review", progress: "Removing", finish: "Done" };
const details = { open: false };
let S = null, root = null;

async function copy() {
  const r = await call("copy_details");
  const text = r.ok ? r.text : "";
  if (!text) return;
  try { await navigator.clipboard.writeText(text); return; } catch (e) { /* no clipboard access: show it instead */ }
  info({ title: "Details", body: text, wide: true });
}

function footer(s) {
  const n = s.nav;
  if (!n.visible) return null;
  if (s.page === "finish") {
    const close = button({ label: n.cancel.label, kind: "primary", onClick: () => call("close") });
    close.dataset.fk = "close";
    return h("div", { class: "foot" }, h("span", { class: "reason" }), close);
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
  const active = document.activeElement;
  const fk = active && active.dataset ? active.dataset.fk : null;
  const card = h("main", { class: "card", id: "card" }, PAGES[s.page](s, { call, details, copy }));
  clear(root).append(steps(s), card, footer(s) || h("div", { class: "foot" }));
  if (fk) {
    const el = root.querySelector(`[data-fk="${CSS.escape(fk)}"]`);
    if (el && !el.disabled) el.focus();
  } else if (!modalOpen() && (!active || active === document.body)) {
    const first = card.querySelector("button:not(:disabled)");
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
    if (S.page === "choose" && S.nav.visible && S.nav.next.enabled) { e.preventDefault(); call("next"); }
  }
}

async function boot() {
  detectIcons();
  root = document.getElementById("setup");
  initModals(document.getElementById("modal-root"), root);
  on("ask", (p) => ask(p));
  on("uninstall_state", (p) => { S = p; render(); });
  document.addEventListener("keydown", onKey);
  document.addEventListener("contextmenu", (e) => e.preventDefault());
  const r = await call("boot");
  if (r.ok) { S = r.state; render(); }
  await call("ready");
}

boot();
