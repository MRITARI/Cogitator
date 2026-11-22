// The Snapshots page: setups saved before risky actions. Names come from files on disk, so every node is built with textContent.
import { call } from "../api.js";
import { promptText } from "../components/modal.js";
import { button } from "../components/widgets.js";
import { h } from "../dom.js";
import { on } from "../store.js";

export const title = "Snapshots";

export function mount(container, ctx) {
  const msg = h("div", { class: "msg tone-bad", role: "alert", hidden: true });
  const list = h("section", { class: "s-group", "aria-label": "Snapshots" });
  const report = (r) => { msg.hidden = !!r.ok; msg.textContent = r.ok ? "" : r.error || "That did not work."; return r; };
  let alive = true, timer = null, keep = 20;

  async function load() {
    const r = await call("snapshots_list");
    list.replaceChildren();
    if (!r.ok) { list.append(h("div", { class: "empty", text: r.error })); return; }
    keep = r.keep || keep;
    if (!r.items.length) list.append(h("div", { class: "empty", text: "No snapshots yet." }));
    for (const s of r.items) {
      list.append(h("div", { class: "s-row" }, h("div", { class: "lbl" }, h("div", { class: "t", text: s.label })),
        h("div", { class: "ctl" }, button({ label: "Restore", kind: "primary", onClick: async () => report(await call("snapshot_restore", s.name)) }))));
    }
  }

  const create = button({ label: "Create snapshot", kind: "primary", onClick: async () => {
    const reason = await promptText({ title: "Save a snapshot", hint: "a few words to remember it by", value: "manual", allowBlank: true });
    if (reason !== null) report(await call("snapshot_create", reason));
  } });
  const prune = button({ label: "Prune", kind: "secondary", title: "Delete the oldest snapshots", onClick: async () => {
    const n = await promptText({ title: "Delete old snapshots", hint: "how many of the newest to keep (at least 1)", value: String(keep) });
    if (n === null) return;
    const v = Number(n.trim());
    report(Number.isInteger(v) ? await call("snapshot_prune", v) : { ok: false, error: "Keep must be a whole number of at least 1." });
  } });
  container.append(h("div", { class: "toolbar", role: "toolbar", "aria-label": "Snapshots" }, create, prune), msg, list);
  const off = on("log", () => { if (!alive) return; clearTimeout(timer); timer = setTimeout(load, 150); });
  load();
  ctx.setHint("");
  ctx.setSummary("");
  return { unmount() { alive = false; off(); clearTimeout(timer); }, focus() { create.focus(); } };
}
