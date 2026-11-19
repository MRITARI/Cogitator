// Page state and the single entry point Python pushes into (window.cogitatorPush). Handlers are registered per payload type.
const handlers = new Map();
const watchers = new Set();

export const state = {
  header: null, order: [], rows: new Map(), summary: "", sort: { col: null, desc: false }, filter: "", emptyHint: "",
  selected: null, logs: [], page: "mods", loaded: false,
};

export function on(type, fn) {
  if (!handlers.has(type)) handlers.set(type, []);
  handlers.get(type).push(fn);
  return () => { const a = handlers.get(type); const i = a.indexOf(fn); if (i >= 0) a.splice(i, 1); };
}

export function watch(fn) {
  watchers.add(fn);
  return () => watchers.delete(fn);
}

export function notify(what, payload) {
  for (const fn of watchers) fn(what, payload);
}

function dispatch(p) {
  switch (p.type) {
    case "state":
      state.order = p.rows.map((r) => r.folder);
      state.rows = new Map(p.rows.map((r) => [r.folder, r]));
      state.summary = p.summary; state.sort = p.sort; state.filter = p.filter; state.emptyHint = p.empty; state.loaded = true;
      if (p.header) state.header = p.header;
      break;
    case "row":
      if (state.rows.has(p.row.folder)) state.rows.set(p.row.folder, p.row);
      state.summary = p.summary;
      break;
    case "header":
      state.header = p.header;
      break;
    case "log":
      state.logs.push({ text: p.text, style: p.style });
      if (state.logs.length > 500) state.logs.splice(0, state.logs.length - 500);
      break;
    default:
      break;
  }
  for (const fn of handlers.get(p.type) || []) fn(p);
  notify(p.type, p);
}

window.cogitatorPush = (batch) => {
  for (const p of Array.isArray(batch) ? batch : [batch]) {
    try { dispatch(p); } catch (e) { console.error(e); }
  }
};

export function applyTheme(choice) {
  const root = document.documentElement;
  if (choice === "light" || choice === "dark") root.dataset.theme = choice;
  else delete root.dataset.theme;
}
