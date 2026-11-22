// Toasts: short action results. Polite live region; errors stay until dismissed. Text only goes through textContent.
import { h } from "../dom.js";
import { icon } from "../icons.js";

const MAX = 4;
let box = null;

export function initToasts(root) {
  box = h("div", { class: "toasts", role: "region", "aria-label": "Notifications", "aria-live": "polite" });
  root.append(box);
}

export function toast(text, tone = "") {
  if (!box || !text) return;
  const t = h("div", { class: `toast ${tone}`, role: tone === "bad" ? "alert" : "status" }, h("span", { class: "msg", text }));
  const close = () => { clearTimeout(timer); t.remove(); };
  let timer = null;
  if (tone === "bad") {
    const x = h("button", { type: "button", class: "x", "aria-label": "Dismiss" }, icon("close"));
    x.addEventListener("click", close);
    t.append(x);
  } else timer = setTimeout(close, 4000);
  box.append(t);
  while (box.childElementCount > MAX) box.firstElementChild.remove();
}
