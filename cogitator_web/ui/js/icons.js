// All icon glyphs live here: Segoe Fluent Icons (Windows 11), else Segoe MDL2 Assets, else a short text fallback.
import { h } from "./dom.js";

const GLYPHS = {
  mods: ["", "M"], profiles: ["", "P"], snapshots: ["", "S"], tools: ["", "T"], settings: ["", "*"],
  sync: ["", "R"], add: ["", "+"], folder: ["", "F"], play: ["", ">"], up: ["", "^"], down: ["", "v"],
  search: ["", "?"], chevronUp: ["", "^"], chevronDown: ["", "v"], star: ["", "*"], pin: ["", "P"],
  note: ["", "n"], check: ["", "ok"], close: ["", "x"], power: ["", "O"], info: ["", "i"],
  dash: ["", "-"], error: ["", "!"],
  grip: ["", "::"], help: ["", "?"], log: ["", "L"], mark: ["", "+"], doctor: ["", "D"],
};

let mode = "fluent";

function installed(family) {
  const c = document.createElement("canvas").getContext("2d");
  const probe = "";
  c.font = `32px serif`;
  const base = c.measureText(probe).width;
  c.font = `32px "${family}", serif`;
  return Math.abs(c.measureText(probe).width - base) > 0.5;
}

export function detectIcons() {
  if (installed("Segoe Fluent Icons")) mode = "fluent";
  else if (installed("Segoe MDL2 Assets")) mode = "mdl2";
  else mode = "text";
  document.documentElement.classList.toggle("icons-text", mode === "text");
  if (mode === "mdl2") document.documentElement.style.setProperty("--icons", '"Segoe MDL2 Assets"');
  return mode;
}

export function icon(name) {
  const g = GLYPHS[name] || ["", ""];
  return h("span", { class: "icon", "aria-hidden": "true", text: mode === "text" ? g[1] : g[0] });
}
