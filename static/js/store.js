// Shared state + tiny event bus.
export const state = { tree: [], envs: [], selection: null };
const bus = new EventTarget();
export const emit = (name, detail) => bus.dispatchEvent(new CustomEvent(name, { detail }));
export const on = (name, fn) => bus.addEventListener(name, (e) => fn(e.detail));
export const store = {
  get(k, d) { try { const v = localStorage.getItem('bar.' + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem('bar.' + k, JSON.stringify(v)); } catch { /* ignore */ } },
};
export function navigate(kind, id) { location.hash = kind ? `#/${kind}/${id}` : '#/'; }
