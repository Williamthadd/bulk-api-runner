// Bootstrap: wire sidebar, editor routing, environments, monitor and health checks.
import { GET, POST, setDbBanner } from './api.js';
import { el, clear, $, toast } from './util.js';
import { state, store } from './store.js';
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

// Sidebar: drag the handle to resize, double-click (or Enter) to collapse/expand. Remembered between visits.
function initSidebarResizer() {
  const sb = $('#sidebar'), rz = $('#sidebarResizer');
  const MIN = 180, MAX = () => Math.max(MIN, Math.min(640, window.innerWidth - 360));
  const setWidth = (w) => { sb.style.width = Math.max(MIN, Math.min(MAX(), w)) + 'px'; };
  const setCollapsed = (c) => { document.body.classList.toggle('sidebar-collapsed', c); store.set('sidebarCollapsed', c); };
  const saved = store.get('sidebarWidth', null);
  if (saved) setWidth(saved);
  setCollapsed(!!store.get('sidebarCollapsed', false));
  rz.addEventListener('mousedown', (e) => {
    e.preventDefault();
    rz.classList.add('dragging'); document.body.classList.add('resizing');
    const left = sb.getBoundingClientRect().left;
    const move = (ev) => {
      const w = ev.clientX - left;
      if (w < MIN / 2) { setCollapsed(true); return; }          // dragged almost shut: collapse
      document.body.classList.remove('sidebar-collapsed'); setWidth(w);
    };
    const up = () => {
      rz.classList.remove('dragging'); document.body.classList.remove('resizing');
      window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up);
      if (document.body.classList.contains('sidebar-collapsed')) store.set('sidebarCollapsed', true);
      else { store.set('sidebarCollapsed', false); store.set('sidebarWidth', parseInt(sb.style.width, 10) || 290); }
    };
    window.addEventListener('mousemove', move); window.addEventListener('mouseup', up);
  });
  const toggle = () => setCollapsed(!document.body.classList.contains('sidebar-collapsed'));
  rz.addEventListener('dblclick', toggle);
  rz.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      e.preventDefault(); document.body.classList.remove('sidebar-collapsed');
      setWidth(sb.getBoundingClientRect().width + (e.key === 'ArrowRight' ? 20 : -20));
      store.set('sidebarCollapsed', false); store.set('sidebarWidth', parseInt(sb.style.width, 10));
    }
  });
  window.addEventListener('resize', () => { if (sb.style.width) setWidth(parseInt(sb.style.width, 10)); });
}
initSidebarResizer();

(async () => {
  initMonitor();
  await Promise.all([loadTree(), loadEnvs(), loadProfiles()]);
  if (!location.hash && state.tree[0] && state.tree[0].requests[0]) location.hash = `#/request/${state.tree[0].requests[0].id}`;
  else route();
  health();
  setInterval(health, 10000);
})();
