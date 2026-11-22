// HTML modals: built hidden, shown with a CSS transition, one at a time (a queue), focus trapped, Esc closes, focus returns.
import { call } from "../api.js";
import { h, focusable, styleClass } from "../dom.js";
import { button } from "./widgets.js";

let root, appEl;
const queue = [];
let current = null;

export function initModals(rootEl, appElement) {
  root = rootEl;
  appEl = appElement;
  document.addEventListener("keydown", (e) => {
    if (!current) return;
    if (e.key === "Escape") {
      e.preventDefault(); e.stopPropagation();
      if (current.dismissable) current.cancel();
    } else if (e.key === "Tab") trap(e);
  }, true);
}

export function modalOpen() { return current !== null; }

function trap(e) {
  const f = focusable(current.dialog);
  if (!f.length) { e.preventDefault(); return; }
  const first = f[0], last = f[f.length - 1], a = document.activeElement;
  if (!current.dialog.contains(a)) { e.preventDefault(); first.focus(); }
  else if (e.shiftKey && a === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && a === last) { e.preventDefault(); first.focus(); }
}

function open(spec) {
  queue.push(spec);
  if (!current) next();
}

function next() {
  const spec = queue.shift();
  if (!spec) return;
  const returnTo = document.activeElement;
  const dialog = h("div", { class: `modal${spec.wide ? " wide" : ""}`, role: "dialog", "aria-modal": "true", "aria-labelledby": "modal-title" });
  const scrim = h("div", { class: "scrim" }, dialog);
  const state = { dialog, dismissable: spec.dismissable !== false, cancel: () => finish(spec.cancelValue) };
  function finish(value) {
    if (current !== state) return;
    current = null;
    scrim.classList.remove("show");
    appEl.inert = false;
    setTimeout(() => scrim.remove(), 200);
    if (returnTo && returnTo.isConnected) returnTo.focus();
    if (spec.done) spec.done(value);
    next();
  }
  spec.build(dialog, finish);
  root.append(scrim);
  appEl.inert = true;
  current = state;
  void scrim.offsetWidth; // start the transition from the hidden state
  scrim.classList.add("show");
  const focusEl = dialog.querySelector("[data-autofocus]") || focusable(dialog)[0];
  if (focusEl) focusEl.focus();
}

const title = (t) => h("h2", { id: "modal-title", text: t });
const bodyEl = (t, mono) => (t ? h("div", { class: `body${mono ? " mono" : ""}`, text: t }) : null);
const af = (b) => { b.dataset.autofocus = "1"; return b; };

// Answer a question that Python asked (confirm / text / choice / notice).
export function ask(p) {
  const send = (v) => call("answer", p.id, v);
  if (p.kind === "confirm") {
    open({ cancelValue: false, done: send, build: (d, fin) => {
      const yes = button({ label: "OK", kind: "primary", onClick: () => fin(true) });
      const no = button({ label: "Cancel", kind: "secondary", onClick: () => fin(false) });
      d.append(title(p.title), bodyEl(p.body), h("div", { class: "foot" }, no, yes));
      af(p.default_yes === false ? no : yes);
    } });
  } else if (p.kind === "text") {
    open({ cancelValue: null, done: send, build: (d, fin) => {
      const input = h("input", { class: "input", type: p.password ? "password" : "text", placeholder: p.hint || "", autocomplete: "off", spellcheck: "false", "aria-label": p.title });
      input.value = p.value || "";
      const ok = button({ label: "OK", kind: "primary", onClick: () => fin(input.value) });
      const sync = () => { ok.disabled = !p.allow_blank && !input.value.trim(); };
      input.addEventListener("input", sync);
      input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !ok.disabled) { e.preventDefault(); fin(input.value); } });
      sync();
      d.append(title(p.title), bodyEl(p.body), h("div", { class: "field" }, input),
        h("div", { class: "foot" }, button({ label: "Cancel", kind: "secondary", onClick: () => fin(null) }), ok));
      af(input);
    } });
  } else if (p.kind === "choice") {
    open({ cancelValue: null, done: send, build: (d, fin) => {
      const sel = p.selected || 0;
      const opts = (p.options || []).map((o, i) => {
        const b = button({ label: o, kind: i === sel ? "primary" : "secondary", onClick: () => fin(i) });
        if (i === sel) af(b);
        return b;
      });
      d.append(title(p.question), h("div", { class: "choices" }, opts), h("div", { class: "foot" }, button({ label: "Cancel", kind: "ghost", onClick: () => fin(null) })));
    } });
  } else if (p.kind === "notice") {
    open({ dismissable: p.dismissable !== false, cancelValue: true, done: send, build: (d, fin) => {
      d.append(title(p.title), bodyEl(p.body), h("div", { class: "foot" }, af(button({ label: p.button || "OK", kind: "primary", onClick: () => fin(true) }))));
    } });
  }
}

// Local information modals (help, doctor report)
export function info({ title: t, body, segments, wide }) {
  open({ wide, cancelValue: true, build: (d, fin) => {
    const content = segments
      ? h("div", { class: "body mono" }, segments.map(([text, style]) => h("span", { class: styleClass(style), text })))
      : bodyEl(body, true);
    d.append(title(t), content, h("div", { class: "foot" }, af(button({ label: "Close", kind: "primary", onClick: () => fin(true) }))));
  } });
}

// Local prompts for the pages: resolve with the text (or null when cancelled).
export function promptText({ title: t, hint, body, value, allowBlank }) {
  return new Promise((resolve) => open({ cancelValue: null, done: resolve, build: (d, fin) => {
    const input = h("input", { class: "input", type: "text", placeholder: hint || "", autocomplete: "off", spellcheck: "false", "aria-label": t });
    input.value = value || "";
    const ok = button({ label: "OK", kind: "primary", onClick: () => fin(input.value) });
    const sync = () => { ok.disabled = !allowBlank && !input.value.trim(); };
    input.addEventListener("input", sync);
    input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !ok.disabled) { e.preventDefault(); fin(input.value); } });
    sync();
    d.append(title(t), bodyEl(body), h("div", { class: "field" }, input), h("div", { class: "foot" }, button({ label: "Cancel", kind: "secondary", onClick: () => fin(null) }), ok));
    af(input);
  } }));
}

// A list of choices for the pages: resolves with the picked item's name (or null when cancelled). Labels come from files on disk: text only.
export function pickFrom({ title: t, items, empty }) {
  return new Promise((resolve) => open({ cancelValue: null, done: resolve, build: (d, fin) => {
    const list = items.length ? h("div", { class: "choices" }, items.map((it) => button({ label: it.label, kind: "secondary", onClick: () => fin(it.name) }))) : bodyEl(empty);
    d.append(title(t), list, h("div", { class: "foot" }, af(button({ label: "Cancel", kind: "secondary", onClick: () => fin(null) }))));
  } }));
}

// A read-only text box with a Select all button (the share code).
export function showCode({ title: t, code, note }) {
  open({ cancelValue: true, build: (d, fin) => {
    const box = h("textarea", { class: "input", readonly: true, rows: 6, spellcheck: "false", "aria-label": t });
    box.value = code;
    const sel = button({ label: "Select all", kind: "secondary", onClick: () => { box.focus(); box.select(); } });
    d.append(title(t), bodyEl(note), h("div", { class: "field" }, box), h("div", { class: "foot" }, sel, af(button({ label: "Close", kind: "primary", onClick: () => fin(true) }))));
  } });
}
