// The Tools page: one card per tool. The flows themselves run in Python and talk through the usual modals.
import { call } from "../api.js";
import { promptText } from "../components/modal.js";
import { button } from "../components/widgets.js";
import { h } from "../dom.js";
import { on, state } from "../store.js";

export const title = "Tools";
export const subtitle = "Diagnose problems and find mods.";

const TOOLS = [
  ["Doctor", "Check the game folder, the loader, the framework and your mods for problems.", "Run", () => call("doctor")],
  ["Find a crashing mod", "Step-by-step wizard that turns mods off in halves to find the one that crashes the game.", "Start", () => call("bisect")],
  ["Crash-log leads", "Look through the game's crash logs for mods that were involved.", "Show", () => call("crashlog")],
  ["Link mods to Nexus", "Match every unlinked mod to its Nexus page so updates can be found (M).", "Link", () => call("link_all")],
  ["Update Mod Loader / Framework", "Install a newer Darktide Mod Loader or Mod Framework when one is available (F).", "Update", () => call("framework_update")],
  ["Update Cogitator", "Look for a newer Cogitator, download it if you agree, and install it.", "Update", () => call("cogitator_update")],
  ["Search Nexus Mods", "Search Nexus Mods by part of a mod's name and install from a result (F3).", "Search", async () => {
    const text = await promptText({ title: "Search Nexus Mods", hint: "part of a mod's name" });
    return text ? call("search_nexus", text) : { ok: true };
  }],
];

export function mount(container, ctx) {
  const msg = h("div", { class: "msg tone-bad", role: "alert", hidden: true });
  const report = (r) => { msg.hidden = !!r.ok; msg.textContent = r.ok ? "" : r.error || "That did not work."; };
  const list = h("section", { class: "s-group", "aria-label": "Tools" });
  let first = null;
  const updateBtns = [];
  for (const [name, desc, label, run] of TOOLS) {
    const b = button({ label, kind: "primary", onClick: async () => report(await run()) });
    if (name === "Update Cogitator") updateBtns.push(b);
    if (!first) first = b;
    list.append(h("div", { class: "s-row" }, h("div", { class: "lbl" }, h("div", { class: "t", text: name }), h("div", { class: "h", text: desc })),
      h("div", { class: "ctl" }, b)));
  }
  const showUpdate = () => {
    const v = state.header && state.header.update_version;
    for (const b of updateBtns) b.textContent = v ? `Update available v${v}` : "Update";
  };
  const off = on("header", showUpdate);
  showUpdate();
  container.append(msg, list);
  ctx.setHint("");
  ctx.setSummary("");
  return { unmount() { off(); }, focus() { first.focus(); } };
}
