// The Settings page: rows with the label and muted help on the left, the control on the right. Changes are saved through
// api.set_setting (Python validates and writes the same settings file the CLI and TUI use).
import { call } from "../api.js";
import { applyBackground } from "../background.js";
import { button, toggleSwitch, setSwitch } from "../components/widgets.js";
import { h } from "../dom.js";
import { applyTheme, watch } from "../store.js";

export const title = "Settings";
export const subtitle = "Your Nexus key, appearance and how Cogitator behaves.";
export const focusIds = { key: "nexus-key" };

const KEY_HELP = "Create one in your Nexus account settings, under API. It is stored in your OS keyring (or a private file) and is never logged or shown.";

function row(label, help, ...ctl) {
  return h("div", { class: "s-row" }, h("div", { class: "lbl" }, h("div", { class: "t", text: label }), help ? h("div", { class: "h", text: help }) : null),
    ctl.length ? h("div", { class: "ctl" }, ...ctl) : null);
}

// the one place in the app where the user may select text: a path they might want to copy
function pick(r) { const el = r.querySelector(".h"); if (el) el.classList.add("pick"); return r; }

function select(label, options, value, onChange) {
  const s = h("select", { class: "input", "aria-label": label }, options.map(([v, t]) => h("option", { value: v, text: t })));
  s.value = value;
  s.addEventListener("change", () => onChange(s.value));
  return s;
}

export function mount(container, ctx) {
  const wrap = h("div", { class: "settings" });
  container.append(wrap);
  let cur = null;
  const err = h("div", { class: "msg tone-bad", role: "alert", hidden: true });

  const fail = (text) => { err.hidden = !text; err.textContent = text || ""; };

  async function save(name, value) {
    const r = await call("set_setting", name, value);
    if (!r.ok) { fail(r.error); return null; }
    fail("");
    cur = r.settings;
    return r.settings;
  }

  function accountText(s) {
    const n = s.nexus;
    if (!n.key_set) return "No API key set.";
    const acct = n.premium === null ? "key set, account type unknown" : (n.premium ? "premium account" : "free account");
    return `${acct}; quota: ${n.quota || "unknown"}`;
  }

  function build(s) {
    cur = s;
    wrap.replaceChildren();
    // ---- Nexus
    const keyMsg = h("div", { class: "msg", role: "status" });
    const key = h("input", { class: "input", id: focusIds.key, type: "password", placeholder: "Paste your Nexus API key", autocomplete: "off", spellcheck: "false", "aria-label": "Nexus API key" });
    const account = h("div", { class: "h", text: accountText(s) });
    const go = button({ label: "Validate and save", kind: "primary", onClick: validate });
    key.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); validate(); } });
    async function validate() {
      const v = key.value;
      key.value = ""; // the key is never kept in the page
      if (!v.trim()) { keyMsg.className = "msg tone-warn"; keyMsg.textContent = "Paste a key first."; return; }
      keyMsg.className = "msg tone-dim"; keyMsg.textContent = "Checking the key with Nexus...";
      go.disabled = true;
      const r = await call("validate_key", v);
      go.disabled = false;
      if (!r.ok) { keyMsg.className = "msg tone-bad"; keyMsg.textContent = r.error; }
    }
    off.push(watch((what, p) => {
      if (what === "key_result") {
        go.disabled = false;
        keyMsg.className = `msg ${p.ok ? "tone-ok" : "tone-bad"}`;
        keyMsg.textContent = p.message;
        call("get_settings").then((r) => { if (r.ok) { cur = r.settings; account.textContent = accountText(r.settings); } });
      }
    }));
    const keyRow = h("div", { class: "s-row" }, h("div", { class: "lbl" }, h("div", { class: "t", text: "Nexus API key" }), h("div", { class: "h", text: KEY_HELP }), keyMsg),
      h("div", { class: "ctl" }, key, go));
    wrap.append(h("section", { class: "s-group", "aria-label": "Nexus" }, keyRow,
      h("div", { class: "s-row" }, h("div", { class: "lbl" }, h("div", { class: "t", text: "Account" }), account))));

    // ---- Appearance
    const bgRow = (st) => {
      const sel = select("Background", [["default", "Default"], ["plain", "Plain"], ["custom", "Custom image"]], st.gui_background, async (v) => {
        const n = await save("gui_background", v);
        if (n) await applyBackground(n.gui_background);
        sel.value = cur.gui_background;
      });
      const apply = async (r) => {
        if (!r.ok) { fail(r.error); return; }
        fail("");
        if (r.cancelled) return;
        cur = r.settings;
        sel.value = cur.gui_background;
        await applyBackground(cur.gui_background);
      };
      return row("Background", "Only in the dark theme. A custom image (PNG, JPG or WEBP, up to 4 MB) is blurred and darkened so text stays readable.", sel,
        button({ label: "Choose image...", icon: "folder", kind: "secondary", onClick: async () => apply(await call("pick_background")) }),
        button({ label: "Reset", kind: "ghost", onClick: async () => apply(await call("reset_background")) }));
    };
    wrap.append(h("section", { class: "s-group", "aria-label": "Appearance" },
      row("Theme", "System follows the Windows setting.", select("Theme", [["system", "System"], ["dark", "Dark"], ["light", "Light"]], s.gui_theme, async (v) => {
        applyTheme(v);
        await save("gui_theme", v);
      })),
      bgRow(s)));

    // ---- Behaviour
    const sw = (label, name, value) => {
      const t = toggleSwitch({ label, checked: value, onChange: async (el) => {
        const want = el.getAttribute("aria-checked") !== "true";
        setSwitch(el, want);
        const r = await save(name, want);
        if (!r) setSwitch(el, !want);
      } });
      return t;
    };
    wrap.append(h("section", { class: "s-group", "aria-label": "Behaviour" },
      row("Check for updates on start", "Ask Nexus about new versions of your mods each time Cogitator opens.", sw("Check for updates on start", "check_updates_on_start", s.check_updates_on_start)),
      row("Allow self-update downloads", "Lets Cogitator download its own updates when the update mode in config.toml is download or auto. Nothing is downloaded without it.", sw("Allow self-update downloads", "update_consent", s.update_consent)),
      row("Repair after a game update", "What to do when a game update has switched the mods off: nothing, ask first, or repair automatically.",
        select("Repair after a game update", [["off", "Off"], ["ask", "Ask"], ["auto", "Automatic"]], s.auto_repair, (v) => save("auto_repair", v)))));

    // ---- Folders
    const dl = h("input", { class: "input", type: "text", readonly: true, value: s.downloads_effective, "aria-label": "Downloads folder", title: s.downloads_effective });
    const show = (n) => { dl.value = n.downloads_effective; dl.title = n.downloads_effective; };
    wrap.append(h("section", { class: "s-group", "aria-label": "Folders" },
      row("Downloads folder", "Where Import zip starts looking, and where the newest download is picked up.", dl,
        button({ label: "Browse", icon: "folder", kind: "secondary", onClick: async () => { const r = await call("pick_downloads"); if (r.ok && r.settings) { cur = r.settings; show(cur); fail(""); } else if (!r.ok) fail(r.error); } }),
        button({ label: "Default", kind: "ghost", onClick: async () => { const n = await save("downloads_dir", ""); if (n) show(n); } }))));

    // ---- About
    wrap.append(h("section", { class: "s-group", "aria-label": "About" },
      row("Version", "Cogitator is not affiliated with Fatshark, Games Workshop or Nexus Mods.", h("span", { class: "pick", text: s.version })),
      pick(row("Log file", s.log_path)), pick(row("Settings folder", s.config_dir))));
    wrap.append(err);
  }

  const off = [];
  call("get_settings").then((r) => { if (r.ok) build(r.settings); else wrap.append(h("div", { class: "empty", text: r.error })); });
  ctx.setHint("");
  ctx.setSummary("");
  return { unmount() { off.forEach((f) => f()); }, focus() { const k = document.getElementById(focusIds.key); if (k) k.focus(); } };
}
