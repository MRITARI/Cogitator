// The Profiles page: named setups of the mods. Names come from files on disk, so every node is built with textContent.
import { call } from "../api.js";
import { promptText, showCode } from "../components/modal.js";
import { button } from "../components/widgets.js";
import { h } from "../dom.js";
import { on } from "../store.js";

export const title = "Profiles";
export const subtitle = "Profiles are named setups of your mods: which are on, and in what order.";

export function mount(container, ctx) {
  const msg = h("div", { class: "msg tone-bad", role: "alert", hidden: true });
  const list = h("section", { class: "s-group", "aria-label": "Profiles" });
  const report = (r) => { msg.hidden = !!r.ok; msg.textContent = r.ok ? "" : r.error || "That did not work."; return r; };
  let alive = true, timer = null;

  async function load() {
    const r = await call("profiles_list");
    list.replaceChildren();
    if (!r.ok) { list.append(h("div", { class: "empty", text: r.error })); return; }
    if (!r.items.length) list.append(h("div", { class: "empty", text: "No profiles yet. Use Save current to keep this setup." }));
    for (const p of r.items) {
      list.append(h("div", { class: "s-row" }, h("div", { class: "lbl" }, h("div", { class: "t", text: p.name }), h("div", { class: "h", text: p.label })),
        h("div", { class: "ctl" },
          button({ label: "Switch", kind: "primary", onClick: async () => report(await call("profile_switch", p.name)) }),
          button({ label: "Delete", kind: "secondary", onClick: async () => { report(await call("profile_delete", p.name)); } }))));
    }
  }

  const save = button({ label: "Save current", kind: "primary", onClick: async () => {
    const name = await promptText({ title: "Profile name", hint: "e.g. hordes" });
    if (name) report(await call("profile_save", name));
  } });
  const share = button({ label: "Share", kind: "secondary", title: "Show a code for the current setup", onClick: async () => {
    const r = report(await call("profile_share"));
    if (r.ok) showCode({ title: "Share code for the current setup", code: r.code, note: "Send this to someone: Profiles > Import." });
  } });
  const imp = button({ label: "Import", kind: "secondary", onClick: async () => {
    const code = await promptText({ title: "Paste a setup code", hint: "cog1:..." });
    if (code) report(await call("profile_import_code", code));
  } });
  container.append(h("div", { class: "toolbar", role: "toolbar", "aria-label": "Profiles" }, save, share, imp), msg, list);
  on("log", () => { if (!alive) return; clearTimeout(timer); timer = setTimeout(load, 150); });
  load();
  ctx.setHint("");
  ctx.setSummary("");
  return { unmount() { alive = false; clearTimeout(timer); }, focus() { save.focus(); } };
}
