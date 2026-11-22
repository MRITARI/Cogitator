// The window background: html[data-bg] picks the layer (see app.css); a custom image arrives as a data: URI from Python.
import { call } from "./api.js";

export async function applyBackground(mode) {
  const bg = document.getElementById("bg");
  let m = mode === "plain" || mode === "custom" ? mode : "default";
  if (m === "custom") {
    const r = await call("get_background");
    if (r.ok && r.data) bg.style.backgroundImage = `url("${r.data}")`;
    else m = "default";
  }
  if (m !== "custom") bg.style.backgroundImage = "";
  document.documentElement.dataset.bg = m;
  return m;
}
