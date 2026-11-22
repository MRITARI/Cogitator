import { h } from "../dom.js";
import { icon } from "../icons.js";

export function button({ label, icon: ic, kind = "outline", title, onClick, disabled, id, iconOnly, trailingIcon }) {
  const b = h("button", { type: "button", class: `btn btn-${kind}${iconOnly ? " btn-icon" : ""}`, title, id, "aria-label": iconOnly ? label : null },
    ic ? icon(ic) : null, iconOnly ? null : label, trailingIcon ? icon(trailingIcon) : null);
  if (disabled) b.disabled = true;
  if (onClick) b.addEventListener("click", onClick);
  return b;
}

// tone: ok | warn | bad | "" ; the dot carries the colour, the text stays readable
export function badge({ tone = "", key, value, primary = false }) {
  return h("span", { class: `badge ${tone}${primary ? " primary" : ""}` }, h("span", { class: "dot" }),
    key ? h("span", { class: "k", text: key }) : null, h("span", { text: value }));
}

export function setBadge(el, { tone = "", key, value }) {
  el.className = `badge ${tone}`;
  el.replaceChildren(...[h("span", { class: "dot" }), key ? h("span", { class: "k", text: key }) : null, h("span", { text: value })].filter(Boolean));
}

export function toggleSwitch({ label, checked, onChange, tabindex }) {
  const s = h("button", { type: "button", class: "switch", role: "switch", "aria-checked": String(!!checked), "aria-label": label, tabindex });
  s.addEventListener("click", (e) => { e.stopPropagation(); if (onChange) onChange(s); });
  return s;
}

export function setSwitch(s, checked, label) {
  s.setAttribute("aria-checked", String(!!checked));
  if (label) s.setAttribute("aria-label", label);
}
