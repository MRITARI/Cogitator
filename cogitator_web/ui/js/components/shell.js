import { h, styleClass } from "../dom.js";
import { icon } from "../icons.js";
import { state, watch } from "../store.js";

const NAV = [["mods", "Mods", "mods", true], ["profiles", "Profiles", "profiles", true], ["snapshots", "Snapshots", "snapshots", true],
  ["tools", "Tools", "tools", true], ["settings", "Settings", "settings", true]];

export function buildSidebar(onNav, version) {
  const items = new Map();
  const nav = h("nav", { class: "sidebar", "aria-label": "Main" },
    h("div", { class: "brand" }, h("img", { class: "mark", src: "img/logo.png", alt: "", width: "28", height: "28" }), h("span", { text: "Cogitator" })));
  for (const [id, label, ic, enabled] of NAV) {
    const b = h("button", { type: "button", class: "nav-item", data: { page: id } }, icon(ic), h("span", { text: label }),
      enabled ? null : h("span", { class: "soon", text: "Coming soon" }));
    if (enabled) b.addEventListener("click", () => onNav(id));
    else { b.disabled = true; b.setAttribute("aria-label", `${label} (Coming soon)`); }
    items.set(id, b);
    nav.append(b);
  }
  nav.append(h("div", { class: "spacer" }), h("div", { class: "ver", text: `Version ${version}` }));
  return {
    el: nav,
    select(id) { for (const [k, b] of items) { if (k === id) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current"); } },
  };
}

export function buildDrawer() {
  let collapsed = true;
  const count = h("span", { text: "" });
  const arrow = h("span", { class: "chev" }, icon("chevronDown"));
  const toggle = h("button", { type: "button", "aria-expanded": "false", "aria-controls": "log-body" }, icon("log"), h("span", { text: "Log" }), count, h("span", { class: "dot-bad", hidden: true, title: "An error was logged" }), arrow);
  const dot = toggle.querySelector(".dot-bad");
  const body = h("div", { class: "drawer-body", id: "log-body", role: "log", "aria-live": "off", tabindex: "0", "aria-label": "Log" });
  const el = h("div", { class: "drawer" }, h("div", { class: "drawer-bar" }, toggle), body);
  const set = (c) => {
    collapsed = c;
    body.hidden = c;
    toggle.setAttribute("aria-expanded", String(!c));
    arrow.replaceChildren(icon(c ? "chevronUp" : "chevronDown"));
    if (!c) dot.hidden = true;
  };
  set(true);
  toggle.addEventListener("click", () => set(!collapsed));
  const add = (l) => {
    const near = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
    body.append(h("div", { class: styleClass(l.style), text: l.text }));
    while (body.childElementCount > 500) body.firstElementChild.remove();
    if (near) body.scrollTop = body.scrollHeight;
    count.textContent = ` (${state.logs.length})`;
  };
  for (const l of state.logs) add(l);
  watch((what) => { if (what === "log") add(state.logs[state.logs.length - 1]); });
  return { el, toggle: () => set(!collapsed), flagError() { if (collapsed) dot.hidden = false; } };
}

export function buildStatusline() {
  const left = h("span", { class: "grow", "aria-live": "polite" });
  const right = h("span", { text: "" });
  let summary = "", nexus = "";
  const draw = () => { right.textContent = [summary, nexus].filter(Boolean).join("  |  "); };
  return {
    el: h("div", { class: "statusline" }, left, right),
    setHint(t) { left.textContent = t || ""; },
    setSummary(t) { summary = t || ""; draw(); },
    setNexus(t) { nexus = t || ""; draw(); },
  };
}

export function pageHead(title, subtitle) {
  return h("header", { class: "page-head" }, h("h1", { text: title }), h("p", { text: subtitle }));
}
