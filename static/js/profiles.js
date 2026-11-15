// Auth profiles: OAuth2 token endpoints (client credentials). The app fetches and refreshes Bearer tokens itself.
import { GET, POST, PATCH, DEL } from './api.js';
import { el, clear, toast, modal, confirmDialog, secretInput } from './util.js';
import { state, emit } from './store.js';

state.profiles = [];

export async function loadProfiles() {
  try { state.profiles = (await GET('/api/auth-profiles')).profiles; } catch (e) { toast(e.message, 'error'); }
  return state.profiles;
}
export const profileById = (id) => state.profiles.find((p) => p.id === id) || null;

export function tokenStatusText(t) {
  if (!t) return 'No token fetched yet. One is fetched automatically the first time it is needed.';
  const mins = Math.round(t.expires_in_s / 60);
  const left = t.expired ? 'expired (a new one is fetched automatically)'
    : t.expires_in_s < 120 ? `expires in ${Math.max(0, t.expires_in_s)} s` : `expires in ${mins} min`;
  return `Current token: ${t.length} characters, fetched ${new Date(t.fetched_at).toLocaleTimeString()}, ${left}.`;
}

// Dropdown of profiles + "Manage…" button, used by the request and folder Auth tabs.
export function profilePicker(selectedId, onChange) {
  const sel = el('select.input', { 'aria-label': 'Auth profile', onChange: (e) => onChange(e.target.value ? Number(e.target.value) : null) },
    el('option', { value: '' }, state.profiles.length ? 'Choose an auth profile…' : 'No auth profiles yet'),
    state.profiles.map((p) => el('option', { value: p.id, selected: p.id === selectedId }, p.name)));
  const manage = el('button.btn', { type: 'button', onClick: () => openProfileManager(selectedId) },
    state.profiles.length ? 'Manage auth profiles…' : '＋ New auth profile');
  return el('div.row', el('div.grow', sel), manage);
}

export async function openProfileManager(focusId) {
  await loadProfiles();
  let selId = focusId && profileById(focusId) ? focusId : (state.profiles[0] ? state.profiles[0].id : 'new');
  const list = el('div.env-list');
  const pane = el('div.grow.col');
  const body = el('div.col',
    el('div.note.note-info', 'An auth profile gets the Bearer token for you from an OAuth2 token endpoint (client credentials). Choose it in the Auth tab of a folder or request and you never paste a token again: the app fetches one when needed, reuses it until shortly before it expires, and fetches a new one after a 401. The client secret is encrypted in the database, never shown again and never exported.'),
    el('div.env-layout', list, pane));

  function drawList() {
    clear(list);
    for (const p of state.profiles) list.appendChild(el('button' + (p.id === selId ? '.sel' : ''), { onClick: () => { selId = p.id; drawList(); drawPane(); } },
      el('span.grow', p.name), p.used_by.length ? el('span.badge.badge-info', String(p.used_by.length)) : ''));
    list.appendChild(el('button' + (selId === 'new' ? '.sel' : ''), { onClick: () => { selId = 'new'; drawList(); drawPane(); } }, el('span.muted', '＋ New auth profile')));
  }

  function drawPane() {
    clear(pane);
    const p = selId === 'new' ? null : profileById(selId);
    const f = p ? { name: p.name, token_url: p.token_url, client_id: p.client_id, grant_type: p.grant_type, scope: p.scope, client_secret: '' }
      : { name: '', token_url: '', client_id: '', grant_type: 'client_credentials', scope: '', client_secret: '' };
    const inp = (key, label, ph, hint) => el('label.field', el('span', label),
      el('input.input' + (key === 'name' ? '' : '.mono'), { value: f[key], placeholder: ph, spellcheck: false, 'data-field': key, onInput: (e) => { f[key] = e.target.value; } }),
      hint ? el('span.hint', hint) : '');
    const secret = secretInput({ placeholder: p ? (p.secret_unreadable ? 'cannot decrypt: enter it again' : '•••••••• saved (type to replace)') : 'client secret',
      onInput: (e) => { f.client_secret = e.target.value; } });
    secret.input.setAttribute('data-field', 'client_secret');
    const status = el('div');
    const showStatus = (node) => { clear(status); if (node) status.appendChild(node); };
    if (p) showStatus(el('div.note' + (p.token ? '.note-ok' : '.note-info'), tokenStatusText(p.token)));

    async function save({ quiet = false } = {}) {
      const payload = { name: f.name.trim(), token_url: f.token_url.trim(), client_id: f.client_id.trim(), grant_type: f.grant_type, scope: f.scope.trim() };
      if (f.client_secret.trim()) payload.client_secret = f.client_secret.trim();
      const saved = p ? await PATCH(`/api/auth-profiles/${p.id}`, payload) : await POST('/api/auth-profiles', payload);
      await loadProfiles(); selId = saved.id; emit('profiles-changed');
      if (!quiet) toast(`Auth profile "${saved.name}" saved`, 'ok');
      return saved;
    }
    async function test() {
      let saved;
      try { saved = await save({ quiet: true }); } catch (e) { toast(e.message, 'error'); return; }
      drawList(); drawPane();
      const st = pane.querySelector('[data-status]');
      clear(st).appendChild(el('div.note.note-info', 'Fetching a token…'));
      try {
        const r = await POST(`/api/auth-profiles/${saved.id}/test`);
        await loadProfiles(); emit('profiles-changed');
        clear(st).appendChild(r.ok
          ? el('div.note.note-ok', `Token received. ${tokenStatusText(r.token).replace('Current token: ', '')}`)
          : el('div.note.note-error', `Could not get a token: ${r.error}`));
      } catch (e) { clear(st).appendChild(el('div.note.note-error', e.message)); }
    }

    status.setAttribute('data-status', '');
    pane.append(
      el('div.row', el('strong.grow', p ? p.name : 'New auth profile'),
        p ? el('button.btn.btn-sm.btn-danger', { onClick: async () => {
          if (!await confirmDialog('Delete auth profile', `Delete "${p.name}"?`, { okLabel: 'Delete', danger: true })) return;
          try { await DEL(`/api/auth-profiles/${p.id}`); await loadProfiles(); selId = state.profiles[0] ? state.profiles[0].id : 'new'; emit('profiles-changed'); drawList(); drawPane(); }
          catch (e) { toast(e.message, 'error'); }
        } }, 'Delete') : ''),
      inp('name', 'Name', 'EAMS-Auth'),
      inp('token_url', 'Token URL (POST)', 'https://…/protocol/openid-connect/token', 'The OAuth2 token endpoint. {{variables}} from the active environment are allowed.'),
      el('div.grid2',
        inp('client_id', 'client_id', 'client id'),
        el('label.field', el('span', 'grant_type'),
          el('select.input', { 'data-field': 'grant_type', onChange: (e) => { f.grant_type = e.target.value; } }, el('option', { value: 'client_credentials', selected: true }, 'client_credentials')))),
      el('label.field', el('span', 'client_secret'), secret.node,
        el('span.hint', p ? 'Leave empty to keep the saved secret.' : 'Stored encrypted. It is never shown again or exported.')),
      inp('scope', 'Scope (optional)', '', 'Only if your token endpoint needs one.'),
      p && p.used_by.length ? el('div.muted', `Used by ${p.used_by.join(', ')}.`) : '',
      status,
      el('div.row', el('div.grow'),
        el('button.btn', { onClick: test, title: 'Save, then fetch a token now to check the settings' }, 'Save & test'),
        el('button.btn.btn-primary', { onClick: async () => { try { await save(); drawList(); drawPane(); } catch (e) { toast(e.message, 'error'); } } }, 'Save')));
  }

  drawList(); drawPane();
  modal({ title: 'Auth profiles', body, wide: true, actions: [{ label: 'Close', value: true }] });
}
