// Bootstrap: wire sidebar, editor routing, environments, monitor and health checks.
import { GET, POST, setDbBanner } from './api.js';
import { el, clear, $, toast } from './util.js';
import { state } from './store.js';
import { loadTree, markActive, newFolder } from './tree.js';
import { loadEnvs } from './envs.js';
import { loadProfiles, openProfileManager } from './profiles.js';
import { openRequest, closeRequest } from './request.js';
import { openFolder } from './folder.js';
import { initMonitor } from './monitor.js';

const editor = $('#editor');

function emptyState() {
  closeRequest();
  clear(editor).appendChild(el('div.empty-state',
    el('h2', 'Bulk API Runner'),
    el('p', 'Pick a request in the sidebar. Upload an .xlsx in the Data tab, check the script in the Script tab, run a dry run, inspect the JSON in Output, then Send.'),
    el('p.muted', 'Tip: create an auth profile (top bar → Auth profiles) and choose it in a folder\'s Auth tab, so tokens are fetched for you. Or paste a token into the bearer_token variable (Manage).')));
}

async function route() {
  const m = /^#\/(request|folder)\/(\d+)$/.exec(location.hash);
  if (!m) { state.selection = null; markActive(); emptyState(); return; }
  state.selection = { kind: m[1], id: Number(m[2]) };
  markActive();
  if (m[1] === 'request') await openRequest(editor, state.selection.id);
  else { closeRequest(); await openFolder(editor, state.selection.id); }
}

async function health() {
  try {
    const h = await GET('/api/health');
    setDbBanner(!h.db, h.db ? '' : `Database unavailable at ${h.location}. Start it with \`docker compose up -d db\`. Changes cannot be saved until it is back.`);
  } catch (e) { setDbBanner(true, e.message); }
}

$('#authProfiles').addEventListener('click', () => openProfileManager());
$('#newCollection').addEventListener('click', () => newFolder(null));
$('#importCollection').addEventListener('click', () => $('#importFile').click());
$('#importFile').addEventListener('change', async (e) => {
  const f = e.target.files[0]; e.target.value = '';
  if (!f) return;
  try {
    const doc = JSON.parse(await f.text());
    const res = await POST('/api/import', doc);
    toast(`Imported "${res.name}"`, 'ok'); await loadTree(); location.hash = `#/folder/${res.id}`;
  } catch (err) { toast(err instanceof SyntaxError ? 'Not a valid JSON file' : err.message, 'error'); }
});
window.addEventListener('hashchange', route);

(async () => {
  initMonitor();
  await Promise.all([loadTree(), loadEnvs(), loadProfiles()]);
  if (!location.hash && state.tree[0] && state.tree[0].requests[0]) location.hash = `#/request/${state.tree[0].requests[0].id}`;
  else route();
  health();
  setInterval(health, 10000);
})();
