// Context menu: opens at the pointer, arrow keys move, Esc / click outside closes, focus returns to the row.
import { h } from "../dom.js";

let open = null;

export function closeMenu(restore = true) {
  if (!open) return;
  const { el, returnTo } = open;
  open = null;
  el.classList.remove("show");
  setTimeout(() => el.remove(), 130);
  if (restore && returnTo && returnTo.isConnected) returnTo.focus();
}

export function showMenu(x, y, items, onPick) {
  closeMenu(false);
  const returnTo = document.activeElement;
  const el = h("div", { class: "menu", role: "menu" });
  for (const it of items) {
    if (!it) { el.append(h("hr")); continue; }
    const b = h("button", { type: "button", role: "menuitem", text: it.label });
    if (!it.enabled) b.disabled = true;
    b.addEventListener("click", () => { closeMenu(true); onPick(it.id); });
    el.append(b);
  }
  document.body.append(el);
  const r = el.getBoundingClientRect();
  el.style.left = `${Math.max(4, Math.min(x, window.innerWidth - r.width - 4))}px`;
  el.style.top = `${Math.max(4, Math.min(y, window.innerHeight - r.height - 4))}px`;
  open = { el, returnTo };
  void el.offsetWidth;
  el.classList.add("show");
  const first = el.querySelector("button:not(:disabled)");
  if (first) first.focus();
}

export function menuOpen() { return open !== null; }

document.addEventListener("keydown", (e) => {
  if (!open) return;
  const btns = [...open.el.querySelectorAll("button:not(:disabled)")];
  const i = btns.indexOf(document.activeElement);
  if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeMenu(true); }
  else if (e.key === "ArrowDown") { e.preventDefault(); e.stopPropagation(); if (btns.length) btns[(i + 1) % btns.length].focus(); }
  else if (e.key === "ArrowUp") { e.preventDefault(); e.stopPropagation(); if (btns.length) btns[(i - 1 + btns.length) % btns.length].focus(); }
  else if (e.key === "Tab") { e.preventDefault(); closeMenu(true); }
}, true);
document.addEventListener("mousedown", (e) => { if (open && !open.el.contains(e.target)) closeMenu(false); }, true);
window.addEventListener("blur", () => closeMenu(false));
