// Thin wrapper around window.pywebview.api. Every call resolves to {ok, ...}; it never throws.
function whenReady() {
  if (window.pywebview && window.pywebview.api) return Promise.resolve();
  return new Promise((res) => window.addEventListener("pywebviewready", () => res(), { once: true }));
}

export async function call(name, ...args) {
  await whenReady();
  try {
    const r = await window.pywebview.api[name](...args);
    return r && typeof r === "object" ? r : { ok: false, error: "No answer from Cogitator." };
  } catch (e) {
    return { ok: false, error: String(e && e.message ? e.message : e) };
  }
}
