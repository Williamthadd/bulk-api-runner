// Collapsible JSON viewer with search highlighting.
import { el } from './util.js';

function hl(text, q) {
  if (!q) return [text];
  const out = []; const lower = text.toLowerCase(); let i = 0, j;
  while ((j = lower.indexOf(q, i)) >= 0) { out.push(text.slice(i, j), el('mark', text.slice(j, j + q.length))); i = j + q.length; }
  out.push(text.slice(i));
  return out;
}

function contains(v, q) {
  if (!q) return false;
  return JSON.stringify(v).toLowerCase().includes(q);
}

function render(value, key, q, last, depth) {
  const comma = last ? '' : ',';
  const keyPart = key !== undefined ? [el('span.k', hl(JSON.stringify(String(key)), q)), el('span.p', ': ')] : [];
  if (value !== null && typeof value === 'object') {
    const isArr = Array.isArray(value);
    const entries = isArr ? value.map((v, i) => [i, v]) : Object.entries(value);
    const open = isArr ? '[' : '{', close = isArr ? ']' : '}';
    if (!entries.length) return el('div', keyPart, el('span.p', open + close + comma));
    const wrap = el('div');
    const collapsed = depth >= 3 && !contains(value, q);
    if (collapsed) wrap.classList.add('collapsed');
    const toggle = () => wrap.classList.toggle('collapsed');
    wrap.append(el('span.tg', { onClick: toggle }, '▾'), ...keyPart, el('span.p', open),
      el('span.ell', { onClick: toggle }, ` … ${entries.length} ${isArr ? 'items' : 'keys'} ` + close + comma));
    const body = el('div.node');
    entries.forEach(([k, v], i) => body.appendChild(render(v, isArr ? undefined : k, q, i === entries.length - 1, depth + 1)));
    wrap.append(body, el('div.closer', el('span.p', close + comma)));
    return wrap;
  }
  let cls = 'z', txt = String(value);
  if (typeof value === 'string') { cls = 's'; txt = JSON.stringify(value); }
  else if (typeof value === 'number') cls = 'n';
  else if (typeof value === 'boolean') cls = 'b';
  else if (value === null) txt = 'null';
  return el('div', keyPart, el('span.' + cls, hl(txt, q)), el('span.p', comma));
}

export function jsonView(value, query = '') {
  return el('div.jv', render(value, undefined, (query || '').toLowerCase(), true, 0));
}

export function setAllCollapsed(root, collapsed) {
  root.querySelectorAll('.jv div').forEach((d) => {
    if (!d.querySelector(':scope > .node')) return;
    d.classList.toggle('collapsed', collapsed && !d.parentElement.classList.contains('jv'));
  });
}
