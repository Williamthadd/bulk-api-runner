// Folder editor: name + auth (inherit / auth profile / Bearer token) + export.
import { GET, PATCH } from './api.js';
import { el, clear, toast, secretInput } from './util.js';
import { emit, on } from './store.js';
import { profilePicker, profileById, openProfileManager, tokenStatusText } from './profiles.js';
import { newRequest, newFolder, exportFolder } from './tree.js';

export async function openFolder(container, id) {
  let f;
  try { f = await GET(`/api/folders/${id}`); } catch (e) { clear(container).appendChild(el('div.empty-state', e.message)); return; }
  clear(container);
  let authType = f.auth.type;
  let tokenVal = f.auth.token_is_reference ? f.auth.token : '';
  let touched = false;
  let profileId = f.auth.profile_id || null;
  const nameInp = el('input.title-input', { value: f.name, 'aria-label': 'Folder name', size: Math.max(12, f.name.length + 2),
    onInput: (e) => { e.target.size = Math.max(12, e.target.value.length + 2); } });
  const tokenBox = el('div.col');

  function drawToken() {
    clear(tokenBox);
    if (authType === 'profile') {
      const p = profileById(profileId);
      tokenBox.append(el('label.field', el('span', 'Auth profile'),
        profilePicker(profileId, (id) => { profileId = id; drawToken(); }),
        el('span.hint', 'Every request in this folder that inherits gets its Bearer token from this profile automatically: fetched when needed, reused until shortly before it expires, fetched again after a 401.')));
      if (p && p.id === f.auth.profile_id) tokenBox.appendChild(el('div.note' + (p.token ? '.note-ok' : '.note-info'), tokenStatusText(p.token), ' ',
        el('button.btn.btn-sm', { onClick: () => openProfileManager(p.id) }, 'Edit profile')));
      return;
    }
    if (authType === 'inherit') {
      tokenBox.appendChild(el('div.note.note-info', f.parent_id
        ? (f.inherited_profile ? `Uses the parent folder's auth profile "${f.inherited_profile}" (automatic token).`
          : f.inherited_token_display ? `Uses the parent folder's token: ${f.inherited_token_display}` : 'No parent folder defines a token yet.')
        : 'Top-level collection without a token: requests inside it cannot be sent until a token or auth profile is set here or on the request.'));
      return;
    }
    const ph = f.auth.has_token && !f.auth.token_is_reference ? '•••••••• saved (type to replace)' : '{{bearer_token}}';
    const s = secretInput({ value: tokenVal, placeholder: ph, onInput: (e) => { tokenVal = e.target.value; touched = true; } });
    if (f.auth.token_is_reference) s.input.type = 'text';
    tokenBox.append(
      el('label.field', el('span', 'Token'), s.node,
        el('span.hint', 'Usually {{bearer_token}}, so replacing that one environment variable updates every request. Paste a token obtained elsewhere, or use "Auth profile" to have the app fetch tokens for you.')),
      f.auth.token_unreadable ? el('div.note.note-error', 'The saved token cannot be decrypted (encryption key changed). Enter it again.') : '');
  }

  const radios = el('div.row',
    ...[['inherit', 'Inherit from parent'], ['profile', 'Auth profile (automatic token)'], ['bearer', 'Bearer Token']].map(([v, label]) =>
      el('label.check', el('input', { type: 'radio', name: 'fauth', value: v, checked: authType === v, onChange: () => { authType = v; drawToken(); } }), label)));

  async function save() {
    const body = { name: nameInp.value.trim(), auth: { type: authType } };
    if (authType === 'profile') {
      if (!profileId) { toast('Choose an auth profile (or create one).', 'error'); return; }
      body.auth.profile_id = profileId;
    }
    if (authType === 'bearer' && (touched || f.auth.token_is_reference)) body.auth.token = tokenVal;
    try { await PATCH(`/api/folders/${id}`, body); toast('Folder saved', 'ok'); emit('tree-reload'); openFolder(container, id); }
    catch (e) { toast(e.message, 'error'); }
  }

  drawToken();
  container.append(
    el('div.req-head',
      el('div.crumbs', ...f.path.slice(0, -1).flatMap((p) => [el('span', p), el('span', '/')]), nameInp),
      el('div.statusline', el('span', 'Folder'), el('div.grow'),
        el('button.btn.btn-sm', { onClick: () => newRequest(f.id) }, '＋ Request'),
        el('button.btn.btn-sm', { onClick: () => newFolder(f.id) }, '＋ Folder'),
        el('button.btn.btn-sm', { onClick: () => exportFolder(f) }, 'Export JSON'),
        el('button.btn.btn-primary.btn-sm', { onClick: save }, 'Save'))),
    el('div.tab-body',
      el('div.card', el('h3', 'Authorization'),
        el('div.col', radios, tokenBox,
          el('div.muted', 'Requests inherit the nearest folder\'s auth (auth profile or Bearer token) unless they set their own.')))));
  current = { drawToken, tokenBox };
  nameInp.addEventListener('keydown', (e) => { if (e.key === 'Enter') save(); });
}

let current = null;   // the open folder editor, redrawn when auth profiles change
on('profiles-changed', () => { if (current && document.body.contains(current.tokenBox)) current.drawToken(); });
