// One builder per wizard page. Each takes the pushed state `s` plus `ctx` ({ draft, call, details, copy }) and returns an array of nodes.
import { h } from "../dom.js";
import { button, toggleSwitch } from "../components/widgets.js";

const MARK = { pending: "○", running: "…", done: "✓", skipped: "–", failed: "!", rolled_back: "↺" };

let rerender = () => {};
export function bindRender(fn) { rerender = fn; }

function fk(el, key) { el.dataset.fk = key; return el; }
const flip = (sw) => sw.getAttribute("aria-checked") !== "true";

function switchRow(label, hint, checked, key, onChange, disabled) {
  const sw = fk(toggleSwitch({ label, checked, onChange }), key);
  if (disabled) sw.disabled = true;
  return h("div", { class: "s-row" }, h("div", { class: "lbl" }, h("div", { class: "t", text: label }), hint ? h("div", { class: "h", text: hint }) : null), h("div", { class: "ctl" }, sw));
}

export function express(s, { call }) {
  const e = s.express;
  return [
    h("h1", { text: "Set up Cogitator" }),
    h("div", { class: `status${!e.loading && !e.ok ? " bad" : ""}`, role: "status", text: e.status }),
    e.ok ? h("ul", { class: "lines" }, e.lines.map((l) => h("li", { text: l }))) : null,
    h("div", { class: "notice", text: s.notice }),
    h("div", { class: "grow" }),
    h("div", { class: "row" },
      fk(Object.assign(button({ label: "Install", kind: "primary", disabled: !e.ok, onClick: () => call("install") }), { className: "btn btn-primary btn-big" }), "install"),
      fk(button({ label: "Retry", kind: "secondary", onClick: () => call("retry_express") }), "retry"),
      h("span", { class: "grow" }),
      fk(button({ label: "Advanced…", kind: "ghost", onClick: () => call("to_wizard") }), "adv")),
    h("p", { class: "notice", text: e.hint }),
  ];
}

export function welcome(s, { call }) {
  return [
    h("h1", { text: "Welcome" }),
    h("div", { class: "notice", text: s.notice }),
    h("div", { class: "group" }, switchRow("I understand", "Needed to continue.", s.accepted, "accept", (sw) => call("accept", flip(sw)))),
  ];
}

export function game(s, { draft, call }) {
  const g = s.game;
  const input = fk(h("input", { class: "input", type: "text", spellcheck: "false", autocomplete: "off", "aria-label": "Darktide folder", placeholder: "Folder that contains bundle and binaries" }), "game");
  input.value = draft.game;
  input.addEventListener("input", () => { draft.game = input.value; });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); call("typed_game", input.value); } });
  input.addEventListener("change", () => call("typed_game", input.value));
  return [
    h("h1", { text: "Your Darktide folder" }),
    h("div", { class: "row" }, input,
      fk(button({ label: "Browse…", kind: "secondary", onClick: () => call("browse_game") }), "browse"),
      fk(button({ label: "Retry", kind: "secondary", onClick: () => call("retry_game") }), "retry")),
    h("div", { class: `status${!g.path && !g.loading && g.message ? " bad" : ""}`, role: "status", text: g.message }),
  ];
}

export function key(s, { draft, call }) {
  const k = s.key;
  const input = fk(h("input", { class: "input", type: draft.show ? "text" : "password", spellcheck: "false", autocomplete: "off", "aria-label": "Nexus Mods API key", placeholder: "Paste your key" }), "key");
  input.value = draft.key;
  input.addEventListener("input", () => { draft.key = input.value; call("set_key", input.value); });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && input.value.trim()) { e.preventDefault(); e.stopPropagation(); call("validate_key"); } });
  return [
    h("h1", { text: "Nexus Mods key (optional)" }),
    h("div", { class: "notice", text: k.blurb }),
    h("div", { class: "row" }, input,
      fk(button({ label: draft.show ? "Hide" : "Show", kind: "secondary", onClick: () => { draft.show = !draft.show; rerender(); } }), "show")),
    h("div", { class: "row" },
      fk(button({ label: "Validate", kind: "primary", disabled: k.busy, onClick: () => call("validate_key") }), "validate"),
      fk(button({ label: "Skip", kind: "secondary", onClick: () => { draft.key = ""; call("skip_key"); } }), "skip"),
      fk(button({ label: "Open the Nexus page", kind: "ghost", onClick: () => call("open_key_page") }), "open")),
    h("div", { class: `status${k.status && !k.ok && !k.busy ? " bad" : ""}`, role: "status", text: k.status }),
  ];
}

export function components(s, { call }) {
  return [
    h("h1", { text: "What to install" }),
    h("div", { class: "group" },
      s.components.map((c) => switchRow(c.title, `${c.description}  Status: ${c.status_text}.`, c.selected, `comp-${c.kind}`, (sw) => call("toggle", c.kind, flip(sw))))),
    h("div", { class: "group" },
      switchRow("Switch mods on", "Patches the game so it loads mods.", s.enable_mods, "opt-enable_mods", (sw) => call("set_option", "enable_mods", flip(sw))),
      switchRow("Desktop icon", null, s.desktop_icon, "opt-desktop_icon", (sw) => call("set_option", "desktop_icon", flip(sw))),
      switchRow("Start Menu entry", null, s.start_menu, "opt-start_menu", (sw) => call("set_option", "start_menu", flip(sw)))),
  ];
}

export function review(s, { call }) {
  const r = s.review;
  const sources = (r.sources || []).map((src) => h("div", { class: "group" },
    h("div", { class: "t", text: `${src.title}:` }),
    h("div", { class: "choice", role: "radiogroup", "aria-label": `${src.title} source` },
      src.options.map((label, i) => {
        const id = `src-${src.kind}-${i}`;
        const radio = fk(h("input", { type: "radio", name: `src-${src.kind}`, id }), id);
        radio.checked = i === src.choice;
        radio.addEventListener("change", () => call("select_source", src.kind, i));
        return h("label", { for: id }, radio, h("span", { text: label }));
      })),
    src.manual ? h("div", { class: "row" },
      src.has_url ? fk(button({ label: "Open Nexus page", kind: "secondary", onClick: () => call("open_source", src.kind) }), `open-${src.kind}`) : null,
      fk(button({ label: "Choose the zip…", kind: "secondary", onClick: () => call("choose_zip", src.kind) }), `zip-${src.kind}`)) : null));
  return [
    h("h1", { text: "Review" }),
    h("div", { class: "status", role: "status", text: r.error || r.status }),
    sources,
    r.rows.map((row) => h("div", { class: "group review-row" }, h("div", { class: "t", text: row.label }),
      row.lines.map((l) => h("div", { class: "h", text: l })), row.warning ? h("div", { class: "warn", text: row.warning }) : null)),
  ];
}

export function progress(s, ctx) {
  const p = s.progress;
  const det = h("details", {}, h("summary", { text: "Details" }), h("pre", { text: p.log.join("\n") }));
  det.open = ctx.details.open;
  det.addEventListener("toggle", () => { ctx.details.open = det.open; });
  const bar = h("progress", { max: 100, "aria-label": "Setup progress" });
  bar.value = Math.round(p.fraction * 100);
  return [
    h("h1", { text: p.cancelling ? "Stopping…" : "Installing" }),
    bar,
    h("ul", { class: "tasks" }, p.steps.map((st) => h("li", { class: `task ${st.state}`, "aria-label": `${st.label}: ${st.state}` },
      h("span", { class: "mark", "aria-hidden": "true", text: MARK[st.state] || "?" }),
      h("div", {}, h("div", { text: st.label }), st.detail ? h("div", { class: "detail", text: st.detail }) : null)))),
    det,
  ];
}

export function finish(s, { call, copy }) {
  const f = s.finish;
  return [
    h("h1", { text: f.title }),
    h("div", { class: "lines" }, f.lines.map((l) => h("div", { text: l }))),
    h("div", { class: "group" },
      switchRow("Show the check-up results", null, f.run_doctor, "opt-run_doctor", (sw) => call("set_option", "run_doctor", flip(sw)), !f.doctor_available),
      switchRow("Start Cogitator now", null, f.launch, "opt-launch", (sw) => call("set_option", "launch", flip(sw)), !f.launch_available)),
    h("div", { class: "row" }, fk(button({ label: "Copy details", kind: "secondary", onClick: copy }), "copy")),
  ];
}

export const PAGES = { express, welcome, game, key, components, review, progress, finish };
