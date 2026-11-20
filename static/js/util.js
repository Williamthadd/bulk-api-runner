// Small DOM + UI helpers (no framework).
export function el(spec, props, ...children) {
  const m = spec.match(/^([a-z0-9]+)?((?:[.#][\w-]+)*)$/i);
  const node = document.createElement((m && m[1]) || 'div');
  if (m && m[2]) for (const part of m[2].match(/[.#][\w-]+/g)) {
    if (part[0] === '.') node.classList.add(part.slice(1)); else node.id = part.slice(1);
  }
  if (props && (typeof props !== 'object' || props instanceof Node || Array.isArray(props))) { children.unshift(props); props = null; }
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith('on')) node.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === 'style' && typeof v === 'object') Object.assign(node.style, v);
    else if (k === 'class') node.className += ' ' + v;
    else if (k === 'html') node.innerHTML = v;
    else if (k in node && !['list', 'type', 'for'].includes(k)) node[k] = v;
    else node.setAttribute(k, v === true ? '' : v);
  }
  append(node, children);
  return node;
}
function append(node, children) {
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    node.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}
export const $ = (sel, root = document) => root.querySelector(sel);
export function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); return node; }

export function toast(msg, kind = '') {
  const t = el('div.toast' + (kind ? '.' + kind : ''), msg);
  $('#toastRoot').appendChild(t);
  setTimeout(() => t.remove(), kind === 'error' ? 6000 : 3000);
}

export function modal({ title, body, actions = [], wide = false, onClose }) {
  const root = $('#modalRoot');
  const close = (v) => { bd.remove(); document.removeEventListener('keydown', esc); onClose && onClose(v); };
  const esc = (e) => { if (e.key === 'Escape') close(null); };
  const foot = el('div.modal-foot');
  const bd = el('div.modal-backdrop', { onMousedown: (e) => { if (e.target === bd) close(null); } },
    el('div.modal' + (wide ? '.wide' : ''), { role: 'dialog', 'aria-modal': 'true', 'aria-label': title },
      el('div.modal-head', title), el('div.modal-body', body), foot));
  for (const a of actions) {
    foot.appendChild(el('button.btn' + (a.primary ? '.btn-primary' : '') + (a.danger ? '.btn-danger' : ''),
      { disabled: a.disabled, onClick: async () => {
        // a button's own onClick may return false to keep the dialog open; a plain `value` (even false, e.g. Cancel) always closes
        if (a.onClick) { const r = await a.onClick(); if (r !== false) close(r); } else close(a.value);
      } },
      a.label));
  }
  document.addEventListener('keydown', esc);
  root.appendChild(bd);
  const f = bd.querySelector('input, select, textarea, .btn-primary'); f && setTimeout(() => f.focus(), 0);
  return { close, foot, root: bd };
}

export function confirmDialog(title, message, { okLabel = 'OK', danger = false } = {}) {
  return new Promise((resolve) => {
    modal({ title, body: el('div', message), onClose: (v) => resolve(!!v),
      actions: [{ label: 'Cancel', value: false }, { label: okLabel, primary: !danger, danger, value: true }] });
  });
}

export function promptDialog(title, label, value = '') {
  return new Promise((resolve) => {
    const inp = el('input.input', { value, onKeydown: (e) => { if (e.key === 'Enter') { m.close(inp.value.trim() || null); } } });
    const m = modal({ title, body: el('label.field', el('span', label), inp), onClose: (v) => resolve(v || null),
      actions: [{ label: 'Cancel', value: null }, { label: 'OK', primary: true, onClick: () => inp.value.trim() || false }] });
    setTimeout(() => { inp.focus(); inp.select(); }, 0);
  });
}

export function fmtTime(iso, withDate = false) {
  if (!iso) return '';
  const d = new Date(iso);
  return withDate ? d.toLocaleString() : d.toLocaleTimeString();
}
export function fmtDuration(ms) {
  if (ms == null) return '';
  if (ms < 1000) return ms + ' ms';
  const s = Math.floor(ms / 1000);
  if (s < 60) return (ms / 1000).toFixed(1) + ' s';
  return Math.floor(s / 60) + 'm ' + String(s % 60).padStart(2, '0') + 's';
}
export function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
export function download(name, text, type = 'application/json') {
  const a = el('a', { href: URL.createObjectURL(new Blob([text], { type })), download: name });
  document.body.appendChild(a); a.click(); a.remove();
}
export async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; }
  catch { const t = el('textarea', { value: text }); document.body.appendChild(t); t.select();
    const ok = document.execCommand('copy'); t.remove(); return ok; }
}
export function secretInput({ value = '', placeholder = '', onInput } = {}) {
  const inp = el('input.input', { type: 'password', value, placeholder, autocomplete: 'off', spellcheck: false, onInput });
  const eye = el('button.btn.btn-sm', { type: 'button', title: 'Show / hide', onClick: () => {
    inp.type = inp.type === 'password' ? 'text' : 'password'; eye.textContent = inp.type === 'password' ? 'Show' : 'Hide'; } }, 'Show');
  return { node: el('div.secret-wrap', inp, eye), input: inp };
}
export function statusClass(code) {
  if (code == null) return 'st-x';
  return 'st-' + String(code)[0];
}
export function prettyBody(text) {
  if (!text) return '';
  const i = text.indexOf('\n\n');
  const tryFmt = (s) => { try { return JSON.stringify(JSON.parse(s), null, 2); } catch { return s; } };
  if (text.trim().startsWith('{') || text.trim().startsWith('[')) return tryFmt(text);
  return i >= 0 ? text.slice(0, i) + '\n\n' + tryFmt(text.slice(i + 2)) : text;
}
