// Monitor panel (bottom): live run status over SSE, log table, history, controls.
import { GET, POST } from './api.js';
import { el, clear, $, toast, fmtTime, fmtDuration, statusClass, prettyBody, confirmDialog, secretInput } from './util.js';
import { store } from './store.js';
import { setVariable } from './envs.js';

const M = { run: null, rows: new Map(), filter: 'all', expanded: new Map(), collapsed: store.get('monCollapsed', false), history: [], drawQueued: false, tokenRef: undefined };
const ACTIVE = new Set(['Running', 'Paused']);
let root, head, banner, body, timer;

export function initMonitor() {
  root = $('#monitor');
  root.style.height = store.get('monHeight', 300) + 'px';
  root.classList.toggle('collapsed', M.collapsed);
  head = el('div.mon-head'); banner = el('div'); body = el('div.mon-body');
  root.append(head, banner, body);
  initResizer();
  connect();
  renderAll();
  // reopen the most recent run so the panel is not empty after a restart
  GET('/api/runs?limit=1').then((d) => { if (!M.run && d.runs[0]) showRun(d.runs[0], { quiet: true }); }).catch(() => {});
}

function connect() {
  const es = new EventSource('/api/events');
  es.onmessage = (e) => {
    let ev; try { ev = JSON.parse(e.data); } catch { return; }
    if (ev.type === 'run') {
      const r = ev.run;
      if (M.run && r.id === M.run.id) { M.run = r; queueDraw(); }
      else if (r.status === 'Running' && (!M.run || !ACTIVE.has(M.run.status))) showRun(r, { quiet: true });
    } else if (ev.type === 'item' && M.run && ev.run_id === M.run.id) {
      M.rows.set(ev.item.seq, ev.item);
      if (M.expanded.has(ev.item.seq)) M.expanded.set(ev.item.seq, null);
      queueDraw();
    }
  };
}

export async function showRun(header, { quiet = false } = {}) {
  M.run = header; M.rows = new Map(); M.expanded = new Map(); M.tokenRef = undefined;
  if (!quiet && M.collapsed) toggleCollapsed();
  renderAll();
  try {
    const d = await GET(`/api/runs/${header.id}`);
    if (!M.run || M.run.id !== header.id) return;
    M.run = { ...d.run, can_retry_failed: d.can_retry_failed };
    for (const it of d.items) M.rows.set(it.seq, it);
    renderAll();
  } catch (e) { if (!quiet) toast(e.message, 'error'); }
}

function queueDraw() {
  if (M.drawQueued) return;
  M.drawQueued = true;
  requestAnimationFrame(() => { M.drawQueued = false; renderAll(); });
}

function toggleCollapsed() {
  M.collapsed = !M.collapsed; store.set('monCollapsed', M.collapsed);
  root.classList.toggle('collapsed', M.collapsed); renderHead();
}

function initResizer() {
  const rz = $('#monitorResizer');
  rz.addEventListener('mousedown', (e) => {
    e.preventDefault(); rz.classList.add('dragging');
    if (M.collapsed) toggleCollapsed();
    const move = (ev) => {
      const ws = root.parentElement.getBoundingClientRect();
      const h = Math.max(80, Math.min(ws.bottom - ev.clientY, ws.height - 140));
      root.style.height = h + 'px';
    };
    const up = () => { rz.classList.remove('dragging'); store.set('monHeight', parseInt(root.style.height, 10)); window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up); };
    window.addEventListener('mousemove', move); window.addEventListener('mouseup', up);
  });
  rz.addEventListener('dblclick', toggleCollapsed);
}

function renderAll() { renderHead(); renderBanner(); renderBody(); tick(); }

function elapsed(r) {
  if (!r || !r.started_at) return '';
  const end = r.finished_at ? new Date(r.finished_at) : new Date();
  return fmtDuration(Math.max(0, end - new Date(r.started_at)));
}

function tick() {
  clearInterval(timer);
  if (M.run && ACTIVE.has(M.run.status)) timer = setInterval(() => { const e = head.querySelector('.elapsed'); if (e) e.textContent = elapsed(M.run); }, 1000);
}

function renderHead() {
  clear(head);
  const r = M.run;
  head.appendChild(el('div.mon-title', { onClick: toggleCollapsed, title: M.collapsed ? 'Expand' : 'Collapse' },
    el('span.chev', M.collapsed ? '▸' : '▾'), 'Monitor'));
  const hist = el('select.input', { style: { maxWidth: '300px', padding: '3px 6px' }, 'aria-label': 'Run history',
    onFocus: loadHistory, onMouseenter: loadHistory,
    onChange: (e) => { const h = M.history.find((x) => String(x.id) === e.target.value); if (h) showRun(h); } },
    el('option', { value: '' }, r ? `Run #${r.id}` : 'Run history…'),
    M.history.filter((h) => !r || h.id !== r.id).map((h) => el('option', { value: h.id }, `#${h.id} · ${h.request_name} · ${h.status} · ${fmtTime(h.started_at, true)}`)));
  head.appendChild(hist);
  if (!r) { head.appendChild(el('span.muted', 'No run selected. Send items from a request to see live progress here.')); return; }
  const total = r.total || 0, done = (r.success_count || 0) + (r.failed_count || 0);
  const pct = total ? (done / total) * 100 : 0, failPct = total ? (r.failed_count / total) * 100 : 0;
  head.append(
    el('strong', r.request_name), el('span.run-status.rs-' + r.status, r.status),
    el('div.progress', { title: `${done}/${total}` }, el('div', { style: { width: pct + '%' } }), el('div.fail-part', { style: { left: (pct - failPct) + '%', width: failPct + '%' } })),
    el('span.mono', `${done}/${total}`),
    el('span.badge.badge-ok', `✓ ${r.success_count}`), el('span.badge.badge-fail', `✗ ${r.failed_count}`),
    r.skipped_count ? el('span.badge.badge-muted', { title: 'Skipped by the script (not sent)' }, `skipped ${r.skipped_count}`) : '',
    el('span.muted', '⏱ ', el('span.elapsed', elapsed(r))),
    r.environment_name ? el('span.muted', r.environment_name) : '',
    el('div.grow'));
  if (r.live !== false && r.status === 'Running') head.appendChild(el('button.btn.btn-sm', { onClick: () => act('pause') }, '❚❚ Pause'));
  if (r.status === 'Paused') head.appendChild(el('button.btn.btn-sm.btn-primary', { onClick: () => act('resume') }, '▶ Resume'));
  if (ACTIVE.has(r.status)) head.appendChild(el('button.btn.btn-sm.btn-danger', { onClick: () => act('stop') }, '■ Stop'));
  if (r.can_retry_failed || (r.live && !ACTIVE.has(r.status) && r.failed_count > 0)) head.appendChild(el('button.btn.btn-sm', { onClick: retryFailed }, '↻ Retry failed only'));
  head.append(
    el('div.seg', ['all', 'failed'].map((f) => el('button' + (M.filter === f ? '.on' : ''), { onClick: () => { M.filter = f; renderAll(); } }, f === 'all' ? 'All' : 'Failed'))),
    el('button.btn.btn-sm', { title: 'Detach this run from the panel (history is kept)', onClick: () => { M.run = null; M.rows = new Map(); renderAll(); } }, 'Clear'),
    el('a.btn.btn-sm', { href: `/api/runs/${r.id}/export.csv`, download: '' }, 'Export CSV'));
}

async function loadHistory() {
  try { M.history = (await GET('/api/runs?limit=50')).runs; } catch { return; }
  const sel = head.querySelector('select');
  if (!sel || document.activeElement === sel && sel.dataset.open) return;
  while (sel.options.length > 1) sel.remove(1);
  for (const h of M.history.filter((h) => !M.run || h.id !== M.run.id))
    sel.appendChild(el('option', { value: h.id }, `#${h.id} · ${h.request_name} · ${h.status} · ${fmtTime(h.started_at, true)}`));
  sel.value = '';
}

async function act(action) {
  try { M.run = { ...M.run, ...(await POST(`/api/runs/${M.run.id}/${action}`)) }; renderAll(); }
  catch (e) { toast(e.message, 'error'); }
}

async function retryFailed() {
  const r = M.run;
  const msg = r.idempotent
    ? `Send the ${r.failed_count} failed item(s) again as a new run?`
    : el('div', el('div.note.note-warn', 'This request is NOT idempotent (e.g. a create). If a "failed" item actually reached the server, retrying it can create a duplicate.'),
      el('p', `Send the ${r.failed_count} failed item(s) again as a new run?`));
  if (!(await confirmDialog('Retry failed items', msg, { okLabel: 'Retry failed only', danger: !r.idempotent }))) return;
  try { const run = await POST(`/api/runs/${r.id}/retry-failed`); showRun(run); }
  catch (e) { toast(e.message, 'error'); }
}

async function tokenRefFor(run) {
  if (M.tokenRef !== undefined) return M.tokenRef;
  M.tokenRef = null;
  try {
    const req = await GET(`/api/requests/${run.request_id}`);
    const m = /^\s*\{\{\s*([\w.-]+)\s*\}\}\s*$/.exec(req.effective_auth.display || '');
    M.tokenRef = m ? m[1] : null;
  } catch { /* ignore */ }
  return M.tokenRef;
}

function renderBanner() {
  clear(banner);
  const r = M.run;
  if (!r) return;
  if (r.status === 'Paused' && (r.pause_reason === '401' || r.pause_reason === 'token')) {
    const box = el('div.note.note-warn.mon-banner');
    box.append(el('div', el('strong', r.banner || 'Token expired or invalid, update the token and click Resume')));
    const ctrl = el('div.row', { style: { marginTop: '6px' } });
    box.appendChild(ctrl);
    banner.appendChild(box);
    tokenRefFor(r).then((ref) => {
      if (!ref) { ctrl.append(el('span', 'Update the token in the request\'s Auth tab, then '), el('button.btn.btn-sm.btn-primary', { onClick: () => act('resume') }, 'Resume')); return; }
      const s = secretInput({ placeholder: `paste a new value for {{${ref}}}` });
      s.node.style.flex = '1';
      ctrl.append(s.node,
        el('button.btn.btn-sm.btn-primary', { onClick: async () => {
          const v = s.input.value.trim();
          if (!v) return toast('Paste a token first', 'error');
          try { await setVariable(ref, v); toast(`{{${ref}}} updated`, 'ok'); await act('resume'); } catch (e) { toast(e.message, 'error'); }
        } }, 'Update token & Resume'),
        el('button.btn.btn-sm', { onClick: () => act('resume') }, 'Resume'));
    });
  } else if (r.status === 'Paused' && r.banner) banner.appendChild(el('div.note.note-warn.mon-banner', r.banner));
  if (r.db_error) banner.appendChild(el('div.note.note-error.mon-banner', r.db_error + '. The run continues; rows are kept in memory.'));
  if (r.unsaved_rows) banner.appendChild(el('div.note.note-warn.mon-banner', `${r.unsaved_rows} log row(s) could not be saved to the database (it was unavailable). Export the CSV now to keep them.`));
  if (r.stop_reason && !ACTIVE.has(r.status)) banner.appendChild(el('div.note.' + (r.status === 'Failed' ? 'note-error' : 'note-info') + '.mon-banner', r.stop_reason));
  if ([...M.rows.values()].some((x) => x.http_status === 202))
    banner.appendChild(el('div.note.note-info.mon-banner', el('strong', '202 Accepted'), ' means the API queued the request. The actual database write is performed asynchronously by a worker, so check the data later.'));
}

function renderBody() {
  clear(body);
  if (!M.run) return;
  let rows = [...M.rows.values()].sort((a, b) => a.seq - b.seq);
  if (M.filter === 'failed') rows = rows.filter((x) => !x.success && x.state === 'done' || x.state === 'paused');
  const tb = el('tbody');
  for (const x of rows) {
    const failed = x.state === 'done' && !x.success;
    const status = x.state === 'sending' ? el('span.status-pill.st-run', '…')
      : x.state === 'retrying' ? el('span.status-pill.st-5', `${x.http_status ?? 'ERR'} ↻`)
      : el('span.status-pill.' + statusClass(x.http_status), x.http_status ?? 'ERR');
    const acc = x.accepted;
    const tr = el('tr.r' + (failed ? '.failed' : ''), { onClick: () => toggleRow(x.seq) },
      el('td.muted', fmtTime(x.created_at)), el('td.mono', String(x.seq)), el('td', x.label),
      el('td.url', { title: x.url }, el('strong.mono', x.method + ' '), x.url),
      el('td', status, acc ? el('span.badge.badge-ok', { style: { marginLeft: '4px' } }, 'accepted') : '',
        x.success && !acc ? el('span', { 'aria-label': 'success', style: { marginLeft: '4px', color: 'var(--ok)' } }, '✓') : '',
        failed ? el('span', { 'aria-label': 'failed', style: { marginLeft: '4px', color: 'var(--fail)' } }, '✗') : ''),
      el('td.mono', fmtDuration(x.duration_ms)),
      el('td.msg', { title: x.message }, acc ? el('span.chips', Object.entries(acc).filter(([k]) => ['upload_id', 'schedule_count', 'batch_count'].includes(k)).map(([k, v]) => el('span.chip', `${k}: ${v}`))) : x.message,
        x.attempts > 1 ? el('span.muted', ` (${x.attempts} attempts)`) : ''));
    tb.appendChild(tr);
    if (M.expanded.has(x.seq)) tb.appendChild(el('tr.exp', el('td', { colSpan: 7 }, expandedView(x))));
  }
  if (!rows.length) tb.appendChild(el('tr', el('td.muted', { colSpan: 7, style: { padding: '14px' } }, M.filter === 'failed' ? 'No failed items.' : (ACTIVE.has(M.run.status) ? 'Waiting for the first response…' : 'No items were sent.'))));
  body.appendChild(el('table.log',
    el('thead', el('tr', ['Time', '#', 'Label', 'Request', 'HTTP', 'Duration', 'Response message'].map((h) => el('th', h)))), tb));
}

function expandedView(x) {
  const full = M.expanded.get(x.seq);
  if (!full) { loadFull(x.seq); return el('div.muted', 'Loading…'); }
  return el('div.exp-grid',
    el('div', el('h4', `Request: ${full.method} ${full.url}`), el('pre.code', 'Authorization: Bearer **** (never stored)\n' + prettyBody(full.request_body || ''))),
    el('div', el('h4', `Response: HTTP ${full.http_status ?? '(none)'}${full.attempts > 1 ? `, ${full.attempts} attempts` : ''}`),
      full.accepted ? el('div.chips', { style: { marginBottom: '6px' } }, Object.entries(full.accepted).map(([k, v]) => el('span.chip', `${k}: ${typeof v === 'object' ? JSON.stringify(v) : v}`))) : '',
      el('pre.code', prettyBody(full.response_body || '') || '(empty)')));
}

async function toggleRow(seq) {
  if (M.expanded.has(seq)) M.expanded.delete(seq); else M.expanded.set(seq, null);
  renderBody();
}
async function loadFull(seq) {
  try {
    const full = await GET(`/api/runs/${M.run.id}/items/${seq}`);
    if (M.expanded.has(seq)) { M.expanded.set(seq, full); renderBody(); }
  } catch (e) { toast(e.message, 'error'); M.expanded.delete(seq); }
}
