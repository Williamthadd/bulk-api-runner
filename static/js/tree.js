// Sidebar: collection -> folder -> request tree with context menu and drag-to-move.
import { GET, POST, PUT, PATCH, DEL } from './api.js';
import { el, clear, $, toast, confirmDialog, promptDialog, download } from './util.js';
import { state, emit, on, store, navigate } from './store.js';

const collapsed = new Set(store.get('collapsed', []));
let drag = null;

export async function loadTree() {
  try { state.tree = (await GET('/api/tree')).tree; } catch (e) { toast(e.message, 'error'); }
  draw();
}

function findParentOfRequest(id, nodes = state.tree) {
  for (const f of nodes) {
    if (f.requests.some((r) => r.id === id)) return f;
    const p = findParentOfRequest(id, f.folders); if (p) return p;
  }
  return null;
}

function draw() {
  const root = clear($('#tree'));
  if (!state.tree.length) root.appendChild(el('div.tree-empty', 'No collections yet. Use ＋ to create one.'));
  for (const f of state.tree) root.appendChild(folderNode(f, 0));
  const rootDrop = el('div.tree-root-drop', {
    onDragover: (e) => { if (drag && drag.kind === 'folder') { e.preventDefault(); } },
    onDrop: (e) => { e.preventDefault(); if (drag && drag.kind === 'folder') move('folder', drag.id, null, null); },
  });
  root.appendChild(rootDrop);
  markActive();
}

export function markActive() {
  document.querySelectorAll('.tree-row').forEach((r) => {
    const s = state.selection;
    r.classList.toggle('active', !!s && r.dataset.kind === s.kind && Number(r.dataset.id) === s.id);
  });
}

function folderNode(f) {
  const isCol = collapsed.has(f.id);
  const row = el('div.tree-row', { draggable: true, 'data-kind': 'folder', 'data-id': f.id, role: 'treeitem',
      'aria-expanded': String(!isCol), title: f.name },
    el('span.caret', { onClick: (e) => { e.stopPropagation(); toggle(f.id); } }, isCol ? '▸' : '▾'),
    el('span', { 'aria-hidden': 'true' }, isCol ? '📁' : '📂'),
    el('span.name', f.name),
    el('button.more', { title: 'More actions', onClick: (e) => { e.stopPropagation(); folderMenu(f, e); } }, '⋯'));
  row.addEventListener('click', () => navigate('folder', f.id));
  row.addEventListener('dblclick', () => toggle(f.id));
  row.addEventListener('contextmenu', (e) => { e.preventDefault(); folderMenu(f, e); });
  dnd(row, { kind: 'folder', id: f.id }, (e) => {
    if (!drag) return null;
    if (drag.kind === 'request') return { mode: 'into', go: () => move('request', drag.id, f.id, null) };
    if (drag.kind === 'folder' && drag.id !== f.id) {
      const r = row.getBoundingClientRect();
      if (e.clientY < r.top + r.height * 0.3) return { mode: 'before', go: () => moveFolderBefore(drag.id, f) };
      return { mode: 'into', go: () => move('folder', drag.id, f.id, null) };
    }
    return null;
  });
  const kids = el('div.tree-children' + (isCol ? '.hidden' : ''), { role: 'group' });
  for (const sub of f.folders) kids.appendChild(folderNode(sub));
  for (const r of f.requests) kids.appendChild(requestNode(r, f));
  if (!f.folders.length && !f.requests.length) kids.appendChild(el('div.tree-empty', 'Empty folder'));
  return el('div', row, kids);
}

function requestNode(r, parent) {
  const row = el('div.tree-row', { draggable: true, 'data-kind': 'request', 'data-id': r.id, role: 'treeitem', title: r.name },
    el('span.caret', ''),
    el('span.method.m-' + r.method, r.method === 'DELETE' ? 'DEL' : r.method),
    el('span.name', r.name),
    el('button.more', { title: 'More actions', onClick: (e) => { e.stopPropagation(); requestMenu(r, e); } }, '⋯'));
  row.addEventListener('click', () => navigate('request', r.id));
  row.addEventListener('contextmenu', (e) => { e.preventDefault(); requestMenu(r, e); });
  dnd(row, { kind: 'request', id: r.id }, () => {
    if (!drag || drag.kind !== 'request' || drag.id === r.id) return null;
    const idx = parent.requests.findIndex((x) => x.id === r.id);
    const from = parent.requests.findIndex((x) => x.id === drag.id);
    return { mode: 'before', go: () => move('request', drag.id, parent.id, from >= 0 && from < idx ? idx - 1 : idx) };
  });
  return row;
}

function siblingsOf(folderId, nodes = state.tree, parent = null) {
  for (const f of nodes) {
    if (f.id === folderId) return { list: nodes, parent };
    const s = siblingsOf(folderId, f.folders, f); if (s) return s;
  }
  return null;
}
function moveFolderBefore(id, target) {
  const s = siblingsOf(target.id);
  const idx = s.list.findIndex((x) => x.id === target.id);
  const from = s.list.findIndex((x) => x.id === id);
  move('folder', id, s.parent ? s.parent.id : null, from >= 0 && from < idx ? idx - 1 : idx);
}

function dnd(row, item, decide) {
  let current = null;
  row.addEventListener('dragstart', (e) => { drag = item; e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', ''); e.stopPropagation(); });
  row.addEventListener('dragend', () => { drag = null; document.querySelectorAll('.drop-into,.drop-before').forEach((n) => n.classList.remove('drop-into', 'drop-before')); });
  row.addEventListener('dragover', (e) => {
    current = decide(e);
    row.classList.remove('drop-into', 'drop-before');
    if (!current) return;
    e.preventDefault(); e.stopPropagation();
    row.classList.add(current.mode === 'into' ? 'drop-into' : 'drop-before');
  });
  row.addEventListener('dragleave', () => row.classList.remove('drop-into', 'drop-before'));
  row.addEventListener('drop', (e) => {
    e.preventDefault(); e.stopPropagation();
    row.classList.remove('drop-into', 'drop-before');
    const c = decide(e); if (c) c.go();
  });
}

async function move(kind, id, parentId, index) {
  try { await POST('/api/move', { kind, id, parent_id: parentId, index }); if (parentId) { collapsed.delete(parentId); saveCollapsed(); } }
  catch (e) { toast(e.message, 'error'); }
  loadTree();
}

function toggle(id) { collapsed.has(id) ? collapsed.delete(id) : collapsed.add(id); saveCollapsed(); draw(); }
function saveCollapsed() { store.set('collapsed', [...collapsed]); }

export function showMenu(e, items) {
  const m = clear($('#ctxmenu'));
  for (const it of items) {
    if (it === '-') { m.appendChild(el('hr')); continue; }
    m.appendChild(el('button' + (it.danger ? '.danger' : ''), { role: 'menuitem', onClick: () => { hideMenu(); it.run(); } }, it.label));
  }
  m.classList.remove('hidden');
  const x = Math.min(e.clientX, window.innerWidth - 210), y = Math.min(e.clientY, window.innerHeight - m.offsetHeight - 10);
  m.style.left = x + 'px'; m.style.top = y + 'px';
}
export function hideMenu() { $('#ctxmenu').classList.add('hidden'); }
document.addEventListener('click', hideMenu);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') hideMenu(); });

export async function newRequest(folderId) {
  const name = await promptDialog('New request', 'Request name', 'New request');
  if (!name) return;
  try {
    const r = await POST('/api/requests', { folder_id: folderId, name, method: 'POST' });
    collapsed.delete(folderId); saveCollapsed();
    await loadTree(); navigate('request', r.id);
  } catch (e) { toast(e.message, 'error'); }
}
export async function newFolder(parentId) {
  const name = await promptDialog(parentId ? 'New folder' : 'New collection', 'Name', parentId ? 'New folder' : 'New collection');
  if (!name) return;
  try {
    const f = await POST('/api/folders', { name, parent_id: parentId });
    if (parentId) { collapsed.delete(parentId); saveCollapsed(); }
    await loadTree(); navigate('folder', f.id);
  } catch (e) { toast(e.message, 'error'); }
}
export async function exportFolder(f) {
  try {
    const res = await fetch(`/api/folders/${f.id}/export`);
    if (!res.ok) throw new Error('Export failed');
    download(`${f.name}.collection.json`, await res.text());
    toast('Exported (secrets are not included)', 'ok');
  } catch (e) { toast(e.message, 'error'); }
}

function folderMenu(f, e) {
  showMenu(e, [
    { label: 'New request', run: () => newRequest(f.id) },
    { label: 'New folder', run: () => newFolder(f.id) },
    '-',
    { label: 'Rename', run: async () => {
      const name = await promptDialog('Rename folder', 'Name', f.name);
      if (name) { try { await PATCH(`/api/folders/${f.id}`, { name }); loadTree(); emit('folder-changed', f.id); } catch (er) { toast(er.message, 'error'); } } } },
    { label: 'Duplicate', run: async () => { try { await POST(`/api/folders/${f.id}/duplicate`); loadTree(); } catch (er) { toast(er.message, 'error'); } } },
    { label: 'Export as JSON', run: () => exportFolder(f) },
    '-',
    { label: 'Delete…', danger: true, run: async () => {
      if (await confirmDialog('Delete folder', `Delete "${f.name}" with all its sub-folders and requests? Run history is kept.`, { okLabel: 'Delete', danger: true })) {
        try { await DEL(`/api/folders/${f.id}`); if (state.selection) navigate(); loadTree(); } catch (er) { toast(er.message, 'error'); }
      } } },
  ]);
}

function requestMenu(r, e) {
  showMenu(e, [
    { label: 'Open', run: () => navigate('request', r.id) },
    { label: 'Rename', run: async () => {
      const name = await promptDialog('Rename request', 'Name', r.name);
      if (name) { try { await PUT(`/api/requests/${r.id}`, { name }); loadTree(); emit('request-renamed', r.id); } catch (er) { toast(er.message, 'error'); } } } },
    { label: 'Duplicate', run: async () => { try { const d = await POST(`/api/requests/${r.id}/duplicate`); await loadTree(); navigate('request', d.id); } catch (er) { toast(er.message, 'error'); } } },
    '-',
    { label: 'Delete…', danger: true, run: async () => {
      if (await confirmDialog('Delete request', `Delete "${r.name}"? Run history is kept.`, { okLabel: 'Delete', danger: true })) {
        try { await DEL(`/api/requests/${r.id}`); if (state.selection && state.selection.id === r.id) navigate(); loadTree(); } catch (er) { toast(er.message, 'error'); }
      } } },
  ]);
}

on('tree-reload', loadTree);
export { findParentOfRequest };
