// DOM building with createElement and text nodes only: text always goes through textContent.
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k === "on") for (const [ev, fn] of Object.entries(v)) el.addEventListener(ev, fn);
    else if (k === "data") for (const [dk, dv] of Object.entries(v)) el.dataset[dk] = dv;
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, String(v));
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export function clear(el) {
  el.replaceChildren();
  return el;
}

export function focusable(root) {
  const sel = 'button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])';
  return [...root.querySelectorAll(sel)].filter((e) => !e.hidden && e.offsetParent !== null);
}

// rich-style words from the shared flows ("bold red", "green", "dim") -> CSS classes
export function styleClass(style) {
  const out = [];
  for (const w of String(style || "").split(/\s+/)) {
    if (w === "bold") out.push("bold");
    else if (w === "green") out.push("tone-ok");
    else if (w === "yellow") out.push("tone-warn");
    else if (w === "red") out.push("tone-bad");
    else if (w === "dim") out.push("tone-dim");
    else if (w === "cyan") out.push("tone-info");
  }
  return out.join(" ");
}
