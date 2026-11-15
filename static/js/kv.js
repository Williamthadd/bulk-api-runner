// Key/value table editor (headers, params).
import { el, clear } from './util.js';

export function kvEditor(rows, { keyLabel = 'Key', valueLabel = 'Value', onChange, validate } = {}) {
  let data = (rows || []).map((r) => ({ key: r.key || '', value: r.value || '', enabled: r.enabled !== false }));
  const tbody = el('tbody');
  const msg = el('div');
  const emit = () => { draw(false); onChange && onChange(data.filter((r) => r.key || r.value)); };
  function draw(full = true) {
    if (data.length === 0 || data[data.length - 1].key || data[data.length - 1].value) data.push({ key: '', value: '', enabled: true });
    const problems = [];
    if (full) clear(tbody);
    data.forEach((r, i) => {
      const bad = validate ? validate(r) : null;
      if (bad) problems.push(bad);
      if (!full) { const tr = tbody.children[i]; if (tr) tr.classList.toggle('bad', !!bad); return; }
      const isBlank = i === data.length - 1 && !r.key && !r.value;
      tbody.appendChild(el('tr' + (bad ? '.bad' : ''),
        el('td.ck', el('input', { type: 'checkbox', checked: r.enabled, disabled: isBlank, title: 'Enabled',
          onChange: (e) => { r.enabled = e.target.checked; emit(); } })),
        el('td', el('input.input.mono', { value: r.key, placeholder: keyLabel,
          onInput: (e) => { const was = isBlank; r.key = e.target.value; if (was) { emit(); draw(); focusRow(i, 1); } else emit(); } })),
        el('td', el('input.input.mono', { value: r.value, placeholder: valueLabel,
          onInput: (e) => { const was = isBlank; r.value = e.target.value; if (was) { emit(); draw(); focusRow(i, 2); } else emit(); } })),
        el('td.act', isBlank ? '' : el('button.icon-btn', { title: 'Remove', onClick: () => { data.splice(i, 1); emit(); draw(); } }, '✕'))));
    });
    if (!full && tbody.children.length !== data.length) return draw(true);
    clear(msg);
    if (problems.length) msg.appendChild(el('div.note.note-error', problems[0]));
  }
  function focusRow(i, col) {
    const inp = tbody.children[i] && tbody.children[i].children[col].querySelector('input');
    if (inp) { inp.focus(); const n = inp.value.length; inp.setSelectionRange(n, n); }
  }
  draw();
  return el('div.col',
    el('table.kv', el('thead', el('tr', el('th', ''), el('th', keyLabel), el('th', valueLabel), el('th', ''))), tbody), msg);
}
