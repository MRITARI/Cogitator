// One builder per uninstall page. Each takes the pushed state `s` plus `ctx` ({ call, details, copy }) and returns an array of nodes.
import { h } from "../dom.js";
import { button, toggleSwitch } from "../components/widgets.js";

const MARK = { pending: "○", running: "…", done: "✓", skipped: "–", failed: "!", rolled_back: "↺" };

function fk(el, key) { el.dataset.fk = key; return el; }
const flip = (sw) => sw.getAttribute("aria-checked") !== "true";

function switchRow(label, hint, checked, key, onChange, disabled) {
  const sw = fk(toggleSwitch({ label, checked, onChange }), key);
  if (disabled) sw.disabled = true;
  return h("div", { class: "s-row" }, h("div", { class: "lbl" }, h("div", { class: "t", text: label }), hint ? h("div", { class: "h", text: hint }) : null), h("div", { class: "ctl" }, sw));
}

export function maintenance(s, { call }) {
  const m = s.maintenance;
  const big = (label, kind, key, enabled, fn) => fk(button({ label, kind, disabled: !enabled, onClick: fn }), key);
  return [
    h("h1", { text: "Cogitator is already installed" }),
    h("div", { class: "status", role: "status", text: m.summary }),
    h("div", { class: "big-actions" },
      big("Repair", "primary", "repair", m.repair, () => call("repair")),
      big("Update", "secondary", "update", m.update, () => call("update")),
      big("Uninstall…", "secondary", "uninstall", m.uninstall, () => call("go_uninstall"))),
    h("div", { class: "mini", text: m.repair_note }),
    m.update_available ? h("div", { class: "group" },
      h("div", { class: "notes", tabindex: "0", role: "region", "aria-label": "Release notes", text: m.notes }),
      h("div", { class: "row" },
        fk(button({ label: "Remind me later", kind: "secondary", disabled: m.busy, onClick: () => call("remind") }), "remind"),
        fk(button({ label: "Skip this version", kind: "secondary", disabled: m.busy, onClick: () => call("skip") }), "skip"))) : null,
    h("div", { class: "status", role: "status", text: m.message }),
  ];
}

export function choose(s, { call }) {
  const c = s.choose;
  const rows = [];
  for (const o of c.options) {
    if (o.name === "order" && !o.enabled) continue; // the mod list is offered only when the model says so
    rows.push(switchRow(o.label, o.note || null, o.checked, `opt-${o.name}`, (sw) => call("toggle", o.name, flip(sw)), !o.enabled));
    if (o.name === "data") rows.push(h("div", { class: "mini sub", text: c.data.length ? `Will remove: ${c.data.join(", ")}` : "Nothing stored yet." }));
    if (o.name === "mods" && o.checked) {
      rows.push(h("div", { class: "sub" }, c.mods.map((mod) => switchRow(mod.name, null, mod.checked, `mod-${mod.name}`, (sw) => call("toggle_mod", mod.name, flip(sw))))));
    }
  }
  return [
    h("h1", { text: "What do you want to remove?" }),
    h("div", { class: "mini", text: c.safe_note }),
    h("div", { class: "group" }, rows),
    c.running ? h("div", { class: "row" },
      h("div", { class: "status bad grow", role: "alert", text: c.running_text }),
      fk(button({ label: "Retry", kind: "secondary", onClick: () => call("retry") }), "retry")) : null,
  ];
}

export function review(s) {
  return [
    h("h1", { text: "Review: this will be removed" }),
    h("div", { class: "mini", text: "Check the list. Nothing is deleted until you press Remove." }),
    s.review.map((g) => h("div", { class: "group review-row" }, h("div", { class: "t", text: g.title }), g.lines.map((l) => h("div", { class: "h", text: l })))),
  ];
}

export function progress(s, ctx) {
  const p = s.progress;
  const det = h("details", {}, h("summary", { text: "Details" }), h("pre", { text: p.log.join("\n") }));
  det.open = ctx.details.open;
  det.addEventListener("toggle", () => { ctx.details.open = det.open; });
  const bar = h("progress", { max: 100, "aria-label": "Uninstall progress" });
  bar.value = Math.round(p.fraction * 100);
  return [
    h("h1", { text: p.cancelling ? "Stopping…" : "Removing" }),
    bar,
    h("ul", { class: "tasks" }, p.steps.map((st) => h("li", { class: `task ${st.state}`, "aria-label": `${st.label}: ${st.state}` },
      h("span", { class: "mark", "aria-hidden": "true", text: MARK[st.state] || "?" }),
      h("div", {}, h("div", { text: st.label }), st.detail ? h("div", { class: "detail", text: st.detail }) : null)))),
    det,
  ];
}

export function finish(s, { copy }) {
  const f = s.finish;
  return [
    h("h1", { text: f.title }),
    h("div", { class: "lines" }, f.lines.map((l) => h("div", { text: l }))),
    h("div", { class: "row" }, fk(button({ label: "Copy details", kind: "secondary", onClick: copy }), "copy")),
  ];
}

export const PAGES = { maintenance, choose, review, progress, finish };
