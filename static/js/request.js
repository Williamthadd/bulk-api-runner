// Request editor: URL bar, tabs (Auth, Headers, Params, Data, Script, Output, Settings), dry run and send.
import { GET, POST, PUT, api } from './api.js';
import { el, clear, toast, modal, secretInput, fmtTime, fmtDuration, copyText, debounce } from './util.js';
import { jsonView, setAllCollapsed } from './jsonview.js';
import { kvEditor } from './kv.js';
import { emit, on, store } from './store.js';
import { activeEnv, setVariable, openEnvManager } from './envs.js';
import { showRun } from './monitor.js';
import { profilePicker, tokenStatusText, openProfileManager } from './profiles.js';

const METHODS = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE'];
const TABS = ['Auth', 'Headers', 'Params', 'Data', 'Script', 'Output', 'Settings'];
const memo = {};            // per request id: { upload, preview, tab }
let R = null;               // current editor state

const isAuthHeader = (k) => (k || '').trim().toLowerCase() === 'authorization';
const refName = (t) => { const m = /^\s*\{\{\s*([\w.-]+)\s*\}\}\s*$/.exec(t || ''); return m ? m[1] : null; };

export async function openRequest(container, id) {
  if (R && R.dirty && R.id !== id) await saveRequest({ quiet: true }).catch(() => {});
  let req;
  try { req = await GET(`/api/requests/${id}`); } catch (e) { clear(container).appendChild(el('div.empty-state', e.message)); R = null; return; }
  const m = memo[id] || (memo[id] = { tab: store.get('tab', 'Data') });
  R = { id, req, draft: draftOf(req), dirty: false, container, m, dry: null, selItem: 0, outQuery: '', jsonQuery: '', authTouched: false };
  try { const d = await GET(`/api/requests/${id}/dry-run`); R.dry = d.result; } catch { /* ignore */ }
  if (R.dry && R.dry.upload && !m.upload) m.upload = R.dry.upload;
  render();
}

function draftOf(req) {
  return {
    name: req.name, method: req.method, url: req.url,
    headers: structuredClone(req.headers), params: structuredClone(req.params),
    auth: { type: req.auth.type, token: req.auth.token_is_reference ? req.auth.token : '', profile_id: req.auth.profile_id || null },
    script: req.script, settings: structuredClone(req.settings),
  };
}

function markDirty() {
  if (!R) return;
  R.dirty = true;
  R.saveDot && R.saveDot.classList.remove('hidden');
  updateActions();
}

export async function saveRequest({ quiet = false } = {}) {
  if (!R) return;
  const d = R.draft;
  if (d.headers.some((h) => isAuthHeader(h.key))) { toast('Remove the Authorization header: the token comes from the Auth tab only.', 'error'); throw new Error('auth header'); }
  const body = { name: d.name, method: d.method, url: d.url, headers: d.headers, params: d.params, script: d.script, settings: d.settings };
  if (d.auth.type === 'profile' && !d.auth.profile_id) { toast('Choose an auth profile in the Auth tab (or create one).', 'error'); throw new Error('no profile'); }
  if (R.authTouched || d.auth.type !== R.req.auth.type || (d.auth.type === 'bearer' && R.req.auth.token_is_reference)) {
    body.auth = { type: d.auth.type };
    if (d.auth.type === 'profile') body.auth.profile_id = d.auth.profile_id;
    if (d.auth.type === 'bearer' && (R.authTouched || R.req.auth.token_is_reference)) body.auth.token = d.auth.token;
  }
  try {
    const res = await PUT(`/api/requests/${R.id}`, body);
    const nameChanged = res.name !== R.req.name || res.method !== R.req.method;
    R.req = { ...R.req, ...res };
    R.dirty = false; R.authTouched = false;
    if (!quiet) toast('Saved', 'ok');
    if (nameChanged) emit('tree-reload');
    R.saveDot && R.saveDot.classList.add('hidden');
    renderStatus(); renderAuth(); updateActions();
  } catch (e) { toast(e.message, 'error'); throw e; }
}

// --------------------------------------------------------------------------- layout
function render() {
  const { req, draft, container } = R;
  clear(container);
  const nameInp = el('input.title-input', { value: draft.name, 'aria-label': 'Request name', size: Math.max(12, draft.name.length + 2),
    onInput: (e) => { draft.name = e.target.value; e.target.size = Math.max(12, e.target.value.length + 2); markDirty(); } });
  const methodSel = el('select.input', { 'aria-label': 'Method', onChange: (e) => { draft.method = e.target.value; markDirty(); } },
    METHODS.map((m) => el('option', { value: m, selected: m === draft.method }, m)));
  const urlInp = el('input.input.url', { value: draft.url, placeholder: '{{base_url}}/api/...', spellcheck: false, 'aria-label': 'URL',
    onInput: (e) => { draft.url = e.target.value; markDirty(); } });
  R.saveDot = el('span.dirty-dot.hidden', { title: 'Unsaved changes' });
  R.btnSave = el('button.btn', { onClick: () => saveRequest().catch(() => {}), title: 'Save (Ctrl/Cmd+S)' }, R.saveDot, 'Save');
  R.btnDry = el('button.btn', { onClick: dryRun, title: 'Run the script without sending anything' }, '▶ Run (dry run)');
  R.btnSendFirst = el('button.btn', { onClick: () => confirmSend('send_first'), title: 'Send only the first item, as a trial' }, 'Send first item only');
  R.btnSend = el('button.btn.btn-primary', { onClick: () => confirmSend('send') }, 'Send');
  R.statusLine = el('div.statusline');
  R.tabBar = el('div.tabs', { role: 'tablist' });
  R.panels = {};
  for (const t of TABS) R.panels[t] = el('div.tab-body.hidden' + (t === 'Script' ? '.flush' : ''), { role: 'tabpanel' });
  container.append(
    el('div.req-head',
      el('div.crumbs', ...req.path.flatMap((p) => [el('span', p), el('span', '/')]), nameInp),
      el('div.urlbar', methodSel, urlInp, R.btnSave, R.btnDry, el('div.btn-group', R.btnSendFirst, R.btnSend)),
      R.statusLine, R.tabBar),
    ...Object.values(R.panels));
  renderAuth(); renderHeaders(); renderParams(); renderData(); renderScript(); renderOutput(); renderSettings();
  renderStatus(); updateActions();
  selectTab(R.m.tab && TABS.includes(R.m.tab) ? R.m.tab : 'Data');
}

function renderTabs() {
  clear(R.tabBar);
  const cnt = (n, bad) => (n ? el('span.count' + (bad ? '.bad' : ''), String(n)) : '');
  const hdrs = R.draft.headers.filter((h) => h.key).length;
  const prms = R.draft.params.filter((h) => h.key).length;
  const out = R.dry ? (R.dry.ok ? cnt(R.dry.items.length) : cnt('!', true)) : '';
  for (const t of TABS) {
    const extra = t === 'Headers' ? cnt(hdrs, R.draft.headers.some((h) => isAuthHeader(h.key))) : t === 'Params' ? cnt(prms) : t === 'Output' ? out : '';
    R.tabBar.appendChild(el('button.tab' + (R.m.tab === t ? '.active' : ''), { role: 'tab', 'aria-selected': String(R.m.tab === t), onClick: () => selectTab(t) }, t, extra));
  }
}

function selectTab(t) {
  R.m.tab = t; store.set('tab', t);
  for (const [k, p] of Object.entries(R.panels)) p.classList.toggle('hidden', k !== t);
  renderTabs();
  if (t === 'Script' && R.cm) setTimeout(() => R.cm.refresh(), 0);
}

function renderStatus() {
  const s = clear(R.statusLine);
  const ea = R.req.effective_auth;
  const env = activeEnv();
  s.append(el('span', 'Environment: ', el('strong', env ? env.name : 'none')));
  if (ea.kind === 'profile') {
    const from = ea.source === 'request' ? '' : ` (from folder)`;
    s.append(ea.profile_problems.length
      ? el('span.badge.badge-fail', { title: ea.profile_problems.join('; ') }, `Auth profile "${ea.profile.name}" is incomplete`)
      : el('span.badge.badge-ok', { title: 'The Bearer token is fetched and refreshed automatically' }, `Auto token: ${ea.profile.name}${from}`));
  } else if (!ea.configured) s.append(el('span.badge.badge-fail', 'No Bearer token configured'));
  else if (ea.empty) s.append(el('span.badge.badge-warn', { title: 'Sending is blocked until the token is set' }, `Token empty${ea.missing_vars.length ? ' (undefined ' + ea.missing_vars.join(', ') + ')' : ''}`));
  else if (ea.token_info && ea.token_info.problem) s.append(el('span.badge.badge-fail', { title: ea.token_info.problem }, 'Token looks incomplete'));
  else if (ea.token_info && ea.token_info.expired) s.append(el('span.badge.badge-fail', 'Token expired'));
  else s.append(el('span.badge.badge-ok', `Bearer token from ${ea.source === 'request' ? 'this request' : 'folder'}${tokenExpiry(ea.token_info)}`));
  const ds = R.req.dry_run;
  if (!ds) s.append(el('span.badge.badge-muted', 'Dry run required'));
  else if (!ds.ok) s.append(el('span.badge.badge-fail', 'Last dry run failed'));
  else if (!ds.valid) s.append(el('span.badge.badge-warn', 'File, script or environment changed: run the dry run again'));
  else s.append(el('span.badge.' + (ds.items ? 'badge-ok' : 'badge-warn'), `Dry run OK: ${ds.items} item(s) ready${ds.skipped ? `, ${ds.skipped} skipped` : ''}`));
}

function tokenExpiry(info) {
  if (!info || info.expires_in_s == null) return '';
  const m = Math.round(info.expires_in_s / 60);
  return m >= 60 ? `, expires ${new Date(info.exp).toLocaleTimeString()}` : `, expires in ${Math.max(1, m)} min`;
}

function tokenFacts(info) {
  if (!info) return null;
  if (info.problem) return el('div.note.note-error', info.problem);
  const bits = [`${info.length} characters`];
  if (info.jwt) bits.push('JWT with 3 parts');
  if (info.exp) {
    const mins = Math.max(1, Math.round(info.expires_in_s / 60));
    bits.push(info.expired ? `EXPIRED at ${new Date(info.exp).toLocaleString()}`
      : `expires ${new Date(info.exp).toLocaleString()}${mins < 120 ? ` (in ${mins} min)` : ''}`);
  }
  return el('div.note.' + (info.expired ? 'note-error' : 'note-ok'), 'Current token: ' + bits.join(' · '));
}

function updateActions() {
  if (!R || !R.btnSend) return;
  const ds = R.req.dry_run;
  let why = '';
  if (R.dirty) why = 'Save your changes and run the dry run again';
  else if (!ds) why = 'Run the dry run first';
  else if (!ds.ok) why = 'The last dry run failed';
  else if (!ds.valid) why = 'Something changed since the last dry run: run it again';
  else if (!ds.items) why = 'The dry run produced no items';
  R.btnSend.disabled = !!why; R.btnSendFirst.disabled = !!why;
  R.btnSend.title = why || 'Send all items'; R.btnSendFirst.title = why || 'Send only the first item';
}

// --------------------------------------------------------------------------- Auth
function profileFacts(ea) {
  if (ea.profile_problems.length) {
    return el('div.note.note-error', `Auth profile "${ea.profile.name}" cannot fetch a token yet: ${ea.profile_problems.join('; ')}. `,
      el('button.btn.btn-sm', { onClick: () => openProfileManager(ea.profile.id) }, 'Edit profile'));
  }
  const test = el('button.btn.btn-sm', { onClick: async () => {
    test.disabled = true; test.textContent = 'Fetching…';
    try {
      const r = await POST(`/api/auth-profiles/${ea.profile.id}/test`);
      if (r.ok) { toast('Token received', 'ok'); refreshMeta(); } else toast(`Could not get a token: ${r.error}`, 'error');
    } catch (e) { toast(e.message, 'error'); }
    test.disabled = false; test.textContent = 'Test now';
  } }, 'Test now');
  return el('div.note.' + (ea.token_info ? 'note-ok' : 'note-info'), tokenStatusText(ea.token_info), ' ', test, ' ',
    el('button.btn.btn-sm', { onClick: () => openProfileManager(ea.profile.id) }, 'Edit profile'));
}

function renderAuth() {
  const p = clear(R.panels.Auth);
  const { draft, req } = R;
  const ea = req.effective_auth;
  const box = el('div.col');
  const draw = () => {
    clear(box);
    if (draft.auth.type === 'profile') {
      box.append(el('label.field', el('span', 'Auth profile'),
        profilePicker(draft.auth.profile_id, (id) => { draft.auth.profile_id = id; R.authTouched = true; markDirty(); }),
        el('span.hint', 'The app fetches the Bearer token from the profile\'s token endpoint, reuses it until shortly before it expires and fetches a new one after a 401. Nothing to paste.')));
      if (!R.dirty && ea.kind === 'profile') box.appendChild(profileFacts(ea));
      return;
    }
    if (draft.auth.type === 'inherit') {
      const src = ea.source.startsWith('folder') ? `folder "${ea.source_name}"` : null;
      if (src && req.auth.type === 'inherit' && ea.kind === 'profile') {
        box.append(el('div.note.note-info', `Inherits auth profile "${ea.profile.name}" from ${src}: the token is fetched automatically.`));
        if (!R.dirty) box.appendChild(profileFacts(ea));
        return;
      }
      box.appendChild(src && req.auth.type === 'inherit'
        ? el('div.note.note-info', `Inherits the Bearer token of ${src}: `, el('code.mono', ea.display || ''))
        : req.auth.type === 'inherit' && !ea.configured
          ? el('div.note.note-warn', 'No parent folder has a Bearer token or auth profile. Choose "Auth profile" or "Bearer Token" above, or set one on the folder.')
          : el('div.note.note-info', 'Uses the token of the nearest parent folder (save to see which one).'));
    } else {
      const ph = req.auth.has_token && !req.auth.token_is_reference ? '•••••••• saved (type to replace)' : '{{bearer_token}}';
      const s = secretInput({ value: draft.auth.token, placeholder: ph, onInput: (e) => { draft.auth.token = e.target.value; R.authTouched = true; markDirty(); } });
      if (refName(draft.auth.token)) s.input.type = 'text';
      box.append(el('label.field', el('span', 'Token'), s.node, el('span.hint', 'Paste only the token (a leading "Bearer " is removed automatically). Supports {{variable}}. Sent as "Authorization: Bearer <token>".')));
      if (req.auth.token_unreadable) box.appendChild(el('div.note.note-error', 'The saved token cannot be decrypted (encryption key changed). Enter it again.'));
    }
    const facts = R.dirty ? null : tokenFacts(ea.token_info);
    if (facts) box.appendChild(facts);
    // quick update of the referenced environment variable
    const ref = refName(ea.display);
    if (ref && !R.dirty) {
      const s = secretInput({ placeholder: ea.empty ? 'paste a token' : '•••••••• (paste to replace)' });
      box.appendChild(el('div.card', el('h3', `Token value: {{${ref}}} in "${(activeEnv() || {}).name || '-'}"`),
        ea.empty ? el('div.note.note-warn', `{{${ref}}} is empty. Sending is blocked until you paste a token.`) : '',
        el('div.row', el('div.grow', s.node),
          el('button.btn.btn-primary', { onClick: async () => {
            if (!s.input.value.trim()) return toast('Paste a token first', 'error');
            try { await setVariable(ref, s.input.value.trim()); toast(`{{${ref}}} updated`, 'ok'); } catch (e) { toast(e.message, 'error'); }
          } }, 'Update token'),
          el('button.btn', { onClick: () => openEnvManager(ref) }, 'Environments…')),
        el('div.muted', { style: { marginTop: '6px' } }, 'Paste a token obtained elsewhere; updating this one variable updates every request that uses it. To stop pasting tokens, use an auth profile instead.')));
    }
  };
  p.append(el('div.card', el('h3', 'Authorization'),
    el('div.col',
      el('div.row', ...[['inherit', 'Inherit from parent'], ['profile', 'Auth profile (automatic token)'], ['bearer', 'Bearer Token']].map(([v, label]) =>
        el('label.check', el('input', { type: 'radio', name: 'rauth', value: v, checked: draft.auth.type === v,
          onChange: () => { draft.auth.type = v; R.authTouched = true; markDirty(); draw(); } }), label))),
      box)));
  draw();
}

// --------------------------------------------------------------------------- Headers / Params
function renderHeaders() {
  const p = clear(R.panels.Headers);
  p.append(
    el('div.note.note-info', 'The app adds "Authorization: Bearer <token>" (from the Auth tab) and, for requests with a JSON body, "Content-Type: application/json". No Accept header is added. Authorization cannot be set here.'),
    kvEditor(R.draft.headers, { keyLabel: 'Header', valueLabel: 'Value',
      validate: (r) => (isAuthHeader(r.key) ? 'An "Authorization" header is not allowed here and will be rejected on save. Set the Bearer token in the Auth tab.' : null),
      onChange: (rows) => { R.draft.headers = rows; markDirty(); renderTabs(); } }));
}
function renderParams() {
  const p = clear(R.panels.Params);
  p.append(
    el('div.note.note-info', 'Default query parameters. A script item\'s "query" (or ctx.call(query=...)) overrides keys with the same name.'),
    kvEditor(R.draft.params, { keyLabel: 'Parameter', valueLabel: 'Value', onChange: (rows) => { R.draft.params = rows; markDirty(); renderTabs(); } }));
}

// --------------------------------------------------------------------------- Data
function renderData() {
  const p = clear(R.panels.Data);
  const { m, req } = R;
  const fileInp = el('input', { type: 'file', accept: '.xlsx,.xlsm', hidden: true, onChange: (e) => e.target.files[0] && upload(e.target.files[0]) });
  const dz = el('div.dropzone',
    el('div', el('strong', 'Drop an .xlsx file here'), ' or ', el('button.btn.btn-sm', { onClick: () => fileInp.click() }, 'Choose file'),
      req.data_source.sample_file ? [' or ', el('button.btn.btn-sm', { onClick: useSample }, `Use sample (${req.data_source.sample_file.split('/').pop()})`)] : ''),
    el('div.muted', { style: { marginTop: '4px' } }, 'Max 10 MB. Uploaded files are kept locally for 24 hours.'), fileInp);
  dz.addEventListener('dragover', (e) => { e.preventDefault(); dz.classList.add('over'); });
  dz.addEventListener('dragleave', () => dz.classList.remove('over'));
  dz.addEventListener('drop', (e) => { e.preventDefault(); dz.classList.remove('over'); const f = e.dataTransfer.files[0]; if (f) upload(f); });
  p.appendChild(dz);
  if (!m.upload) {
    p.appendChild(el('div.muted', 'No file selected. Requests without a data source can still be dry-run (the script receives an empty rows list).'));
    return;
  }
  const pv = m.preview;
  const sheetSel = el('select.input', { onChange: (e) => changeSource({ sheet: e.target.value }) },
    (pv ? pv.sheets : [req.data_source.sheet || '']).map((s) => el('option', { value: s, selected: pv && s === pv.sheet }, s)));
  const hdrInp = el('input.input', { type: 'number', min: 1, value: req.data_source.header_row || 1, style: { width: '80px' },
    onChange: (e) => changeSource({ header_row: Math.max(1, parseInt(e.target.value, 10) || 1) }) });
  p.appendChild(el('div.card.row.wrap',
    el('div.grow', el('strong', m.upload.name), el('span.muted', `  ${(m.upload.size / 1024).toFixed(1)} KB, uploaded ${fmtTime(m.upload.created_at, true)}`)),
    el('label.row', el('span', 'Sheet'), sheetSel),
    el('label.row', el('span', 'Header row'), hdrInp)));
  if (!pv) { p.appendChild(el('div.muted', 'Loading preview…')); loadPreview(); return; }
  p.appendChild(el('div.muted', `${pv.total_rows} data row(s) (empty rows skipped). Showing the first ${Math.min(50, pv.total_rows)}.`));
  const head = el('tr', el('th.rownum', 'row'), pv.columns.map((c) => el('th', c, el('span.type', pv.types[c]))));
  const rows = pv.rows.map((r) => el('tr', el('td.rownum', String(r._row)),
    pv.columns.map((c) => (r[c] == null ? el('td.null', 'empty') : el('td', { title: String(r[c]) }, String(r[c]))))));
  p.appendChild(el('div.table-wrap', el('table.grid', el('thead', head), el('tbody', rows))));
}

async function upload(file) {
  if (file.size > 10 * 1024 * 1024) return toast('File is larger than 10 MB', 'error');
  const fd = new FormData(); fd.append('file', file);
  try { const res = await api('POST', `/api/requests/${R.id}/upload`, fd); afterUpload(res); }
  catch (e) { toast(e.message, 'error'); }
}
async function useSample() {
  try { afterUpload(await POST(`/api/requests/${R.id}/use-sample`)); } catch (e) { toast(e.message, 'error'); }
}
function headerRowReset(pv) {
  if (!pv.header_row_reset_from) return;
  R.req.data_source = { ...R.req.data_source, header_row: pv.header_row };
  toast(`Header row ${pv.header_row_reset_from} is beyond the end of this sheet, so it was set back to ${pv.header_row}.`, 'warn');
}
function afterUpload(res) {
  R.m.upload = res.upload; R.m.preview = res.preview;
  R.req.data_source = { ...R.req.data_source, sheet: res.preview.sheet === res.preview.sheets[0] && !R.req.data_source.sheet ? null : res.preview.sheet };
  headerRowReset(res.preview);
  toast(`Loaded ${res.upload.name}: ${res.preview.total_rows} rows`, 'ok');
  renderData(); refreshMeta();
}
async function loadPreview() {
  const ds = R.req.data_source;
  try {
    const q = new URLSearchParams({ header_row: ds.header_row || 1, request_id: R.id }); if (ds.sheet) q.set('sheet', ds.sheet);
    const res = await GET(`/api/uploads/${R.m.upload.id}/preview?${q}`);
    R.m.preview = res.preview; headerRowReset(res.preview); renderData(); if (res.preview.header_row_reset_from) refreshMeta();
  } catch (e) {
    if (e.status === 404) { R.m.upload = null; R.m.preview = null; }
    toast(e.message, 'error'); renderData();
  }
}
async function changeSource(patch) {
  try {
    const res = await PUT(`/api/requests/${R.id}`, { data_source: { ...R.req.data_source, ...patch } });
    R.req = { ...R.req, ...res }; R.m.preview = null; renderData(); renderStatus(); updateActions();
  } catch (e) { toast(e.message, 'error'); }
}

// --------------------------------------------------------------------------- Script
function renderScript() {
  const p = clear(R.panels.Script);
  const host = el('div.script-wrap');
  p.appendChild(host);
  host.appendChild(el('div.script-toolbar',
    el('strong', 'transform(rows, ctx)'), el('span.muted', 'Python, runs in a separate process'), el('div.grow'),
    el('button.btn.btn-sm', { onClick: () => saveRequest().catch(() => {}) }, 'Save'),
    el('button.btn.btn-sm.btn-primary', { onClick: dryRun }, '▶ Run (dry run)')));
  const area = el('div', { style: { flex: '1', minHeight: '0', display: 'flex', flexDirection: 'column' } });
  host.appendChild(area);
  host.appendChild(el('div.script-help', { html:
    'Return a list of <code>{"label": str, "body": {...}}</code> (optional <code>"query"</code>, <code>"headers"</code>). ' +
    '<code>rows</code>: dicts keyed by header text, native types, plus <code>_row</code>. ' +
    '<code>ctx.log(msg)</code> · <code>ctx.skip(label, reason)</code> · <code>ctx.warn(label, msg)</code> · <code>ctx.vars</code> (non-secret variables) · ' +
    '<code>ctx.call(request_name, query=, body=, path=)</code> (GET/HEAD only in a dry run). Scripts run with your privileges: this is not a security sandbox.' }));
  R.cm = window.CodeMirror(area, {
    value: R.draft.script, mode: 'python', lineNumbers: true, indentUnit: 4, tabSize: 4, indentWithTabs: false,
    matchBrackets: true, autoCloseBrackets: true, styleActiveLine: true, gutters: ['CodeMirror-linenumbers', 'err-gutter'],
    extraKeys: {
      Tab: (cm) => (cm.somethingSelected() ? cm.indentSelection('add') : cm.replaceSelection('    ', 'end')),
      'Shift-Tab': (cm) => cm.indentSelection('subtract'),
      'Ctrl-S': () => saveRequest().catch(() => {}), 'Cmd-S': () => saveRequest().catch(() => {}),
      'Ctrl-Enter': () => dryRun(), 'Cmd-Enter': () => dryRun(),
    },
  });
  R.cm.getWrapperElement().style.flex = '1';
  R.cm.on('change', () => { R.draft.script = R.cm.getValue(); clearErrorLine(); markDirty(); });
  if (R.dry && !R.dry.ok && R.dry.line) markErrorLine(R.dry.line);
}
function clearErrorLine() {
  if (R.errLine == null) return;
  R.cm.removeLineClass(R.errLine, 'background', 'cm-error-line');
  R.cm.clearGutter('err-gutter'); R.errLine = null;
}
function markErrorLine(line) {
  if (!R.cm || !line) return;
  clearErrorLine();
  const i = Math.max(0, Math.min(line - 1, R.cm.lineCount() - 1));
  R.cm.addLineClass(i, 'background', 'cm-error-line');
  R.cm.setGutterMarker(i, 'err-gutter', el('span.cm-error-gutter', { title: 'Error here' }, '●'));
  R.errLine = i;
}
function gotoLine(line) {
  selectTab('Script');
  setTimeout(() => { R.cm.refresh(); R.cm.setCursor({ line: line - 1, ch: 0 }); R.cm.scrollIntoView({ line: line - 1, ch: 0 }, 120); R.cm.focus(); }, 30);
}

// --------------------------------------------------------------------------- dry run
async function dryRun() {
  if (!R) return;
  if (R.dirty) { try { await saveRequest({ quiet: true }); } catch { return; } }
  const btns = [R.btnDry]; btns.forEach((b) => { b.disabled = true; b.textContent = 'Running…'; });
  try {
    R.dry = await POST(`/api/requests/${R.id}/dry-run`, { upload_id: R.m.upload ? R.m.upload.id : null });
    R.selItem = 0;
    if (R.dry.ok) { clearErrorLine(); toast(`Dry run OK: ${R.dry.items.length} item(s), ${R.dry.skipped.length} skipped`, 'ok'); }
    else { if (R.dry.line) markErrorLine(R.dry.line); toast('Script failed: ' + R.dry.error, 'error'); }
    renderOutput(); selectTab('Output');
  } catch (e) { toast(e.message, 'error'); }
  finally { btns.forEach((b) => { b.disabled = false; b.textContent = '▶ Run (dry run)'; }); }
  refreshMeta();
}

async function refreshMeta() {
  if (!R) return;
  try {
    const r = await GET(`/api/requests/${R.id}`);
    R.req.dry_run = r.dry_run; R.req.effective_auth = r.effective_auth; R.req.auth = r.auth; R.req.data_source = r.data_source;
    renderStatus(); updateActions(); if (!R.dirty) renderAuth(); renderTabs();
    if (R.m.tab === 'Output') renderOutputValidity();
  } catch { /* banner handles DB errors */ }
}

// --------------------------------------------------------------------------- Output
function renderOutputValidity() {
  if (!R.validityBox) return;
  clear(R.validityBox);
  const ds = R.req.dry_run;
  if (R.dry && R.dry.ok && ds && !ds.valid)
    R.validityBox.appendChild(el('div.note.note-warn', 'The file, script, sheet/header row or environment changed after this dry run. Run it again before sending.'));
}

function renderOutput() {
  const p = clear(R.panels.Output);
  const d = R.dry;
  renderTabs();
  if (!d) { p.appendChild(el('div.empty-state', el('h2', 'No dry run yet'), 'Upload a file in the Data tab, then click ', el('strong', 'Run (dry run)'), '. Nothing is sent to the API.')); return; }
  R.validityBox = el('div'); p.appendChild(R.validityBox); renderOutputValidity();
  const warnLabels = new Map();
  for (const w of d.warnings) { if (!warnLabels.has(w.label)) warnLabels.set(w.label, []); warnLabels.get(w.label).push(w.msg); }
  p.appendChild(el('div.summary',
    stat('Rows read', d.rows_read), stat('Items ready', d.ok ? d.items.length : 0, d.ok ? 'ok' : ''), stat('Skipped', d.skipped.length, d.skipped.length ? 'fail' : ''),
    stat('Warnings', d.warnings.length, d.warnings.length ? 'warn' : ''), stat('Duration', fmtDuration(d.duration_ms)),
    stat('File', d.upload ? d.upload.name : 'none'), stat('Environment', d.environment || 'none')));
  if (!d.ok) {
    p.appendChild(el('div.card',
      el('div.row', el('strong', { style: { color: 'var(--fail)' } }, 'Script failed: nothing will be sent'), el('div.grow'),
        d.line ? el('button.btn.btn-sm', { onClick: () => gotoLine(d.line) }, `Show line ${d.line} in the editor`) : ''),
      el('pre.code.err', { style: { marginTop: '8px' } }, d.traceback || d.error)));
  }
  if (d.ok || d.skipped.length) {
    const list = el('div.list');
    const search = el('input.input.search', { placeholder: 'Filter items…', value: R.outQuery, onInput: debounce((e) => { R.outQuery = e.target.value; drawList(); }, 120) });
    const detail = el('div.detail');
    const drawList = () => {
      clear(list);
      const q = R.outQuery.toLowerCase();
      const items = d.items.map((it, i) => [it, i]).filter(([it]) => !q || it.label.toLowerCase().includes(q) || JSON.stringify(it.body).toLowerCase().includes(q));
      list.appendChild(el('div.sect', `Items to send (${items.length}${q ? ' of ' + d.items.length : ''})`));
      for (const [it, i] of items) {
        const w = warnLabels.get(it.label);
        list.appendChild(el('div.it' + (i === R.selItem ? '.sel' : ''), { onClick: () => { R.selItem = i; drawList(); drawDetail(); } },
          el('span.muted.mono', String(i + 1)), el('span.lbl', it.label), w ? el('span.badge.badge-warn', { title: w.join('\n') }, '⚠ ' + w.length) : ''));
      }
      const sk = d.skipped.filter((s) => !q || (s.label + s.reason).toLowerCase().includes(q));
      if (sk.length) {
        list.appendChild(el('div.sect.bad', `Skipped, not sent (${sk.length})`));
        for (const s of sk) list.appendChild(el('div.it.skipped', el('strong', s.label), el('span.why', s.reason)));
      }
    };
    const drawDetail = () => {
      clear(detail);
      const it = d.items[R.selItem];
      if (!it) { detail.appendChild(el('div.empty-state', d.items.length ? 'Select an item' : 'No valid items')); return; }
      let viewRoot;
      const jq = el('input.input', { placeholder: 'Search JSON…', value: R.jsonQuery, style: { width: '180px' },
        onInput: debounce((e) => { R.jsonQuery = e.target.value; drawBody(); }, 150) });
      const body = el('div.detail-body');
      const drawBody = () => {
        clear(body);
        const w = warnLabels.get(it.label);
        if (w) body.appendChild(el('div.note.note-warn', { style: { marginBottom: '8px' } }, w.map((x) => el('div', '⚠ ' + x))));
        if (it.query) body.append(el('div.muted', 'query'), jsonView(it.query, R.jsonQuery), el('div.muted', { style: { marginTop: '6px' } }, 'body'));
        if (it.headers) body.append(el('div.muted', 'headers'), jsonView(it.headers, R.jsonQuery));
        viewRoot = jsonView(it.body, R.jsonQuery);
        body.appendChild(viewRoot);
      };
      detail.append(el('div.detail-head',
        el('strong.grow', `#${R.selItem + 1} ${it.label}`), jq,
        el('button.btn.btn-sm', { onClick: () => setAllCollapsed(body, false) }, 'Expand all'),
        el('button.btn.btn-sm', { onClick: () => setAllCollapsed(body, true) }, 'Collapse'),
        el('button.btn.btn-sm', { onClick: async () => { await copyText(JSON.stringify(it.body, null, 2)); toast('JSON copied'); } }, 'Copy JSON'),
        el('button.btn.btn-sm', { onClick: () => copyCurl(R.selItem) }, 'Copy as cURL')), body);
      drawBody();
    };
    p.appendChild(el('div.split', el('div.itemlist', search, list), detail));
    drawList(); drawDetail();
  }
  p.appendChild(el('div.card', el('h3', `Script log (${d.logs.length})`),
    d.logs.length ? el('pre.code.logs', d.logs.join('\n')) : el('div.muted', 'Nothing logged. Use ctx.log(...) or print(...).')));
}
const stat = (k, v, cls = '') => el('div.stat' + (cls ? '.' + cls : ''),
  el('div.v' + (String(v).length > 8 ? '.small' : ''), { title: String(v) }, String(v)), el('div.k', k));

async function copyCurl(index) {
  try {
    const r = await POST(`/api/requests/${R.id}/curl`, { index });
    await copyText(r.curl);
    toast(r.errors.length ? 'cURL copied (note: ' + r.errors[0] + ')' : 'cURL copied (token and secrets masked)', r.errors.length ? '' : 'ok');
  } catch (e) { toast(e.message, 'error'); }
}

// --------------------------------------------------------------------------- Settings
function renderSettings() {
  const p = clear(R.panels.Settings);
  const s = R.draft.settings;
  const num = (obj, key, label, hint, attrs = {}) => el('label.field', el('span', label),
    el('input.input', { type: 'number', value: obj[key] ?? '', ...attrs, onInput: (e) => { obj[key] = e.target.value === '' ? null : Number(e.target.value); markDirty(); } }),
    hint ? el('span.hint', hint) : '');
  const chk = (obj, key, label, hint) => el('div.col', { style: { gap: '2px' } },
    el('label.check', el('input', { type: 'checkbox', checked: !!obj[key], onChange: (e) => { obj[key] = e.target.checked; markDirty(); } }), label),
    hint ? el('span.muted', { style: { paddingLeft: '22px', fontSize: '12px' } }, hint) : '');
  const list = (obj, key, label, hint) => el('label.field', el('span', label),
    el('input.input.mono', { value: (obj[key] || []).join(', '), onInput: (e) => { obj[key] = e.target.value.split(/[\s,]+/).filter(Boolean).map(Number).filter((n) => !isNaN(n)); markDirty(); } }),
    hint ? el('span.hint', hint) : '');
  const grid = (...kids) => el('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: '12px' } }, ...kids);
  p.append(
    el('div.card', el('h3', 'Sending'), grid(
      num(s, 'delay_ms', 'Delay between requests (ms)', 'Default 0', { min: 0 }),
      num(s, 'timeout_s', 'Timeout per request (s)', 'Default 30', { min: 1 }),
      num(s, 'script_timeout_s', 'Script timeout (s)', `Empty = server default (${R.req.default_script_timeout_s}s)`, { min: 1 })),
      el('div.col', { style: { marginTop: '12px' } },
        chk(s, 'stop_on_error', 'Stop on first error'),
        chk(s, 'pause_on_401', 'Pause on 401', 'The run pauses at the item that got 401 so you can paste a new token and Resume. Off = 401 is an ordinary failure.'),
        chk(s, 'idempotent', 'Request is idempotent (safe to repeat)', 'Used to warn before "Retry failed only" and before sending.'))),
    el('div.card', el('h3', 'Retry'), grid(
      num(s.retry, 'max_retries', 'Max retries', '0 = no automatic retry', { min: 0, max: 10 }),
      list(s.retry, 'on_status', 'Retry on HTTP status', 'e.g. 503. 400/401/403/404/409 are never retried.'),
      num(s.retry, 'backoff_ms', 'Backoff (ms)', 'Doubles after each attempt (max 30 s)', { min: 0 }))),
    el('div.card', el('h3', 'Success criteria'), grid(
      el('div', chk(s.success, 'require_2xx', 'Require HTTP 2xx')),
      el('label.field', el('span', 'JSON code field'), el('input.input.mono', { value: s.success.code_field || '', placeholder: 'code',
        onInput: (e) => { s.success.code_field = e.target.value; markDirty(); } }), el('span.hint', 'Empty = do not check the body')),
      list(s.success, 'code_values', 'Accepted code values', 'If the JSON body has a numeric code field it must be one of these')),
      el('div.note.note-info', { style: { marginTop: '10px' } }, 'This backend wraps responses as {code, msg, data}; some errors come back as HTTP 200 with a code other than 200, so they count as failures.')),
    el('div.row', el('div.grow'), el('button.btn.btn-primary', { onClick: () => saveRequest().catch(() => {}) }, 'Save settings')));
}

// --------------------------------------------------------------------------- Send
async function confirmSend(mode) {
  if (!R) return;
  if (R.dirty) { toast('Save your changes and run the dry run again first', 'error'); return; }
  let validOnly = false;
  const body = el('div.col');
  let m;
  const draw = async () => {
    let pf;
    try { pf = await GET(`/api/requests/${R.id}/send-check?mode=${mode}&send_valid_only=${validOnly}`); }
    catch (e) { clear(body).appendChild(el('div.note.note-error', e.message)); return; }
    clear(body);
    body.append(el('dl.facts',
      el('dt', 'Request'), el('dd', pf.request_name),
      el('dt', 'Items'), el('dd', `${pf.count}${mode === 'send_first' ? ' (first item only, trial)' : ''}`),
      el('dt', 'Method / URL'), el('dd.mono', `${pf.method} ${pf.url}`),
      el('dt', 'Environment'), el('dd', pf.environment || 'none'),
      el('dt', 'Auth'), el('dd', pf.auth_label || `Bearer token (from ${pf.token_source})`)));
    if (pf.skipped && mode === 'send') body.appendChild(el('label.check.note.note-warn',
      el('input', { type: 'checkbox', checked: validOnly, onChange: (e) => { validOnly = e.target.checked; draw(); } }),
      `${pf.skipped} item(s) were skipped by the script. Send valid items only`));
    for (const b of pf.blockers) body.appendChild(el('div.note.note-error', b));
    for (const w of pf.warnings) body.appendChild(el('div.note.note-warn', w));
    if (pf.environment && /prod/i.test(pf.environment)) body.appendChild(el('div.note.note-warn', el('strong', 'Production environment.'), ' Double-check before sending.'));
    const btn = m.foot.querySelector('.btn-primary');
    btn.disabled = !pf.ok;
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        const run = await POST(`/api/requests/${R.id}/send`, { mode, fingerprint: pf.fingerprint, send_valid_only: validOnly });
        m.close(true); showRun(run); toast(`Run #${run.id} started`, 'ok');
      } catch (e) { toast(e.message, 'error'); btn.disabled = false; }
    };
  };
  m = modal({ title: mode === 'send_first' ? 'Send first item only?' : 'Send items?', body,
    actions: [{ label: 'Cancel', value: false }, { label: mode === 'send_first' ? 'Send 1 item' : 'Send', primary: true, disabled: true, onClick: () => false }] });
  await draw();
}

// --------------------------------------------------------------------------- events
document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's' && R && document.body.contains(R.container.firstChild)) {
    e.preventDefault(); saveRequest().catch(() => {});
  }
});
on('env-changed', () => { if (R) refreshMeta(); });
on('profiles-changed', () => { if (R) { renderAuth(); refreshMeta(); } });
on('request-renamed', (id) => { if (R && R.id === id) openRequest(R.container, id); });
window.addEventListener('beforeunload', (e) => { if (R && R.dirty) { e.preventDefault(); e.returnValue = ''; } });
export const currentRequestId = () => (R ? R.id : null);
export function closeRequest() { R = null; }
