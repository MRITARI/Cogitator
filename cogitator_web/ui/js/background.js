// The window background: html[data-bg] picks the layer (see app.css).
export function applyBackground(mode) {
  const m = mode === "plain" ? "plain" : "default";
  document.documentElement.dataset.bg = m;
  return m;
}
