// Environment selector (top bar) + manager dialog.
import { GET, POST, PUT, PATCH, DEL } from './api.js';
import { el, clear, $, toast, modal, confirmDialog, promptDialog, secretInput } from './util.js';
import { state, emit } from './store.js';

export async function loadEnvs() {
  try { state.envs = (await GET('/api/environments')).environments; } catch (e) { toast(e.message, 'error'); }
  drawSelect();
}
export const activeEnv = () => state.envs.find((e) => e.is_active) || null;

function drawSelect() {
  const sel = clear($('#envSelect'));
  if (!state.envs.length) sel.appendChild(el('option', { value: '' }, 'No environment'));
  for (const e of state.envs) sel.appendChild(el('option', { value: e.id, selected: e.is_active }, e.name));
}

$('#envSelect').addEventListener('change', async (ev) => {
  try { await POST(`/api/environments/${ev.target.value}/activate`); await loadEnvs(); emit('env-changed'); toast(`Environment: ${activeEnv().name}`); }
  catch (e) { toast(e.message, 'error'); }
});
$('#envManage').addEventListener('click', () => openEnvManager());

export function openEnvManager(focusKey) {
  let selId = (activeEnv() || state.envs[0] || {}).id;
  let rows = [];
  const list = el('div.env-list');
  const pane = el('div.grow.col');
  const body = el('div.col',
    el('div.note.note-info', 'Variables are used as {{name}} in the URL, headers, params and the Bearer Token field. Secret values are encrypted in the database, masked here and never exported. Leave a saved secret empty to keep it.'),
    el('div.env-layout', list, pane));

  function drawList() {
    clear(list);
    for (const e of state.envs) list.appendChild(el('button' + (e.id === selId ? '.sel' : ''), { onClick: () => { selId = e.id; drawList(); drawPane(); } },
      el('span.grow', e.name), e.is_active ? el('span.badge.badge-info', 'active') : ''));
    list.appendChild(el('button', { onClick: async () => {
      const name = await promptDialog('New environment', 'Name', 'Production');
      if (!name) return;
      try { const e = await POST('/api/environments', { name }); await loadEnvs(); selId = e.id; drawList(); drawPane(); emit('env-changed'); } catch (er) { toast(er.message, 'error'); }
    } }, el('span.muted', '＋ New environment')));
  }

  function drawPane() {
    clear(pane);
    const env = state.envs.find((e) => e.id === selId);
    if (!env) { pane.appendChild(el('div.muted', 'Create an environment to start.')); return; }
    rows = env.variables.map((v) => ({ ...v, wasSecret: v.is_secret, newValue: v.is_secret ? '' : v.value }));
    const tbody = el('tbody');
    const drawRows = () => {
      clear(tbody);
      if (!rows.length || rows[rows.length - 1].key) rows.push({ key: '', newValue: '', is_secret: false, has_value: false, isNew: true });
      rows.forEach((r, i) => {
        let valCell;
        if (r.is_secret) {
          const ph = r.unreadable ? 'cannot decrypt: re-enter' : r.has_value ? '•••••••• saved (type to replace)' : 'empty';
          const s = secretInput({ value: r.newValue || '', placeholder: ph, onInput: (e) => { r.newValue = e.target.value; r.touched = true; } });
          if (r.unreadable) s.input.classList.add('invalid');
          valCell = s.node;
          if (focusKey && r.key === focusKey) setTimeout(() => s.input.focus(), 50);
        } else {
          valCell = el('input.input.mono', { value: r.newValue || '', placeholder: 'value', onInput: (e) => { r.newValue = e.target.value; r.touched = true; } });
        }
        tbody.appendChild(el('tr',
          el('td', el('input.input.mono', { value: r.key, placeholder: 'variable', onInput: (e) => { const was = !r.key; r.key = e.target.value; if (was && r.key) drawRowsKeep(i); } })),
          el('td', valCell),
          el('td.ck', el('input', { type: 'checkbox', checked: r.is_secret, title: 'Secret', onChange: (e) => { r.is_secret = e.target.checked; r.touched = true; drawRows(); } })),
          el('td.act', r.key ? el('button.icon-btn', { title: 'Remove', onClick: () => { rows.splice(i, 1); drawRows(); } }, '✕') : '')));
      });
    };
    const drawRowsKeep = (i) => { drawRows(); const inp = tbody.children[i].querySelector('input'); inp.focus(); inp.setSelectionRange(inp.value.length, inp.value.length); };
    drawRows();
    pane.append(
      el('div.row', el('strong.grow', env.name),
        env.is_active ? el('span.badge.badge-info', 'active') : el('button.btn.btn-sm', { onClick: async () => { await POST(`/api/environments/${env.id}/activate`); await loadEnvs(); drawList(); drawPane(); emit('env-changed'); } }, 'Set active'),
        el('button.btn.btn-sm', { onClick: async () => { const n = await promptDialog('Rename environment', 'Name', env.name); if (n) { try { await PATCH(`/api/environments/${env.id}`, { name: n }); await loadEnvs(); drawList(); drawPane(); } catch (e) { toast(e.message, 'error'); } } } }, 'Rename'),
        el('button.btn.btn-sm.btn-danger', { onClick: async () => { if (await confirmDialog('Delete environment', `Delete "${env.name}" and its variables?`, { okLabel: 'Delete', danger: true })) { await DEL(`/api/environments/${env.id}`); await loadEnvs(); selId = (state.envs[0] || {}).id; drawList(); drawPane(); emit('env-changed'); } } }, 'Delete')),
      el('table.kv', el('thead', el('tr', el('th', 'Variable'), el('th', 'Value'), el('th', 'Secret'), el('th', ''))), tbody),
      el('div.row', el('div.grow'), el('button.btn.btn-primary', { onClick: save }, 'Save variables')));
  }

  async function save() {
    const vars = rows.filter((r) => r.key.trim()).map((r) => ({
      key: r.key.trim(), is_secret: r.is_secret,
      // empty field on a saved secret = keep the stored value (null); otherwise send what was typed
      value: r.newValue ? r.newValue : (!r.isNew && r.has_value && (r.is_secret || r.wasSecret) ? null : ''),
    }));
    try { await PUT(`/api/environments/${selId}/variables`, { variables: vars }); await loadEnvs(); drawPane(); emit('env-changed'); toast('Variables saved', 'ok'); }
    catch (e) { toast(e.message, 'error'); }
  }

  drawList(); drawPane();
  modal({ title: 'Environments', body, wide: true, actions: [{ label: 'Close', value: true }] });
}

// Update one variable quickly (e.g. a fresh bearer_token while a run is paused).
export async function setVariable(key, value) {
  const env = activeEnv();
  if (!env) throw new Error('No active environment');
  await PUT(`/api/environments/${env.id}/variables/${encodeURIComponent(key)}`, { value });
  await loadEnvs();
  emit('env-changed');
}
