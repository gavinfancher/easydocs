// Shared by the admin panel (/admin/) and the editor (/admin/edit):
// doc/folder management (dialogs, "⋯" menus, drag & drop) and the uploads library.

const $ = id => document.getElementById(id);

function toast(msg, ms = 2500) {
  let t = $('toast');
  if (!t) { t = Object.assign(document.createElement('div'), { id: 'toast', className: 'toast' }); document.body.append(t); }
  t.textContent = msg; t.classList.add('show');
  clearTimeout(t._timer); t._timer = setTimeout(() => t.classList.remove('show'), ms);
}

async function logout() {
  await fetch('/admin/api/logout', { method: 'POST' });
  location.href = '/';
}

const baseName = p => p.split('/').pop();
const parentOf = p => p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : '';
const join = (folder, name) => folder ? folder + '/' + name : name;
const fileSlug = title => slugify(title).replace(/_/g, '-').replace(/-+/g, '-').replace(/^-|-$/g, '') || 'untitled';
const cleanFolderName = n => n.replace(/[\/\\]+/g, ' ').trim();
const editorUrl = path => '/admin/edit#' + encodeURIComponent(path);

// ---------- doc tree management ----------

/**
 * The page plugs into these. The editor overrides them to keep the open doc in sync.
 *   active()                -> path to highlight in the tree
 *   href(path)              -> where clicking a doc goes
 *   defaultFolder()         -> folder preselected in "New doc"
 *   beforeChange(path)      -> called before `path` (or a folder containing it) is renamed/moved
 *   afterMove(src, dst)     -> something moved
 *   afterDelete(path)       -> a doc was deleted
 *   afterCreate(path)       -> a doc was created
 *   openContent(path)       -> unsaved editor content for `path`, if it's open
 *   contentReplaced(path, content) -> `path`'s content was rewritten (title change)
 *   afterRefresh()          -> the tree was reloaded
 */
const docHooks = {
  active: () => null,
  href: editorUrl,
  defaultFolder: () => '',
  beforeChange: async () => { },
  afterMove: () => { },
  afterDelete: () => { },
  afterCreate: path => { location.href = editorUrl(path); },
  openContent: () => undefined,
  contentReplaced: () => { },
  afterRefresh: () => { },
};

let tree = [];
let treeEl = null;

async function refreshTree() {
  tree = await api('/api/tree');
  if (!treeEl) return;
  renderTree(treeEl, tree, { href: docHooks.href, active: docHooks.active(), editable: true });
  if (!tree.length) treeEl.innerHTML = '<div class="empty">No docs yet. Click <b>New doc</b> to start.</div>';
  docHooks.afterRefresh();
}

function findItem(path, items = tree) {
  for (const it of items) {
    if (it.path === path) return it;
    if (it.type === 'folder') { const f = findItem(path, it.children); if (f) return f; }
  }
  return null;
}

/** Folder choices for a dropdown; `exclude` hides a folder and everything inside it. */
function folderOptions(exclude = null) {
  const out = [{ value: '', label: 'Top level (no folder)' }];
  const walk = (items, depth) => {
    for (const it of items) {
      if (it.type !== 'folder' || it.path === exclude) continue;
      out.push({ value: it.path, label: '    '.repeat(depth) + prettyName(it.name) });
      walk(it.children, depth + 1);
    }
  };
  walk(tree, 1);
  return out;
}

function expandFolder(folder) {
  if (folder) { collapsed.delete(folder); saveCollapsed(); }
}

async function newDoc(folder) {
  const r = await appDialog({
    title: 'New doc',
    fields: [
      { name: 'title', label: 'Title', placeholder: 'e.g. Setup guide' },
      { name: 'folder', label: 'Folder', type: 'select', options: folderOptions(), value: folder ?? docHooks.defaultFolder() },
    ],
    okLabel: 'Create doc',
  });
  if (!r || !r.title) return;
  try {
    const res = await api('/admin/api/docs', jsonOpts('POST', { path: join(r.folder, fileSlug(r.title)), content: `# ${r.title}\n\n` }));
    expandFolder(r.folder);
    await refreshTree();
    await docHooks.afterCreate(res.path);
  } catch (err) { toast(err.message); }
}

async function newFolder(parent) {
  const r = await appDialog({
    title: 'New folder',
    fields: [
      { name: 'name', label: 'Name', placeholder: 'e.g. Guides' },
      { name: 'parent', label: 'Inside', type: 'select', options: folderOptions(), value: parent ?? docHooks.defaultFolder() },
    ],
    okLabel: 'Create folder',
  });
  const name = r && cleanFolderName(r.name);
  if (!name) return;
  try {
    await api('/admin/api/folders', jsonOpts('POST', { path: join(r.parent, name) }));
    expandFolder(r.parent);
    await refreshTree();
  } catch (err) { toast(err.message); }
}

async function moveItem(src, destFolder) {
  const dst = join(destFolder, baseName(src));
  if (dst === src) return;
  try {
    await docHooks.beforeChange(src);
    const res = await api('/admin/api/move', jsonOpts('POST', { src, dst }));
    expandFolder(destFolder);
    docHooks.afterMove(src, res.path);
    await refreshTree();
    toast(`Moved to ${destFolder ? prettyName(baseName(destFolder)) : 'top level'}`);
  } catch (err) { toast(err.message); }
}

async function moveDialog(path, kind) {
  const r = await appDialog({
    title: kind === 'folder' ? 'Move folder' : 'Move doc',
    fields: [{ name: 'folder', label: 'Move to', type: 'select', value: parentOf(path),
               options: folderOptions(kind === 'folder' ? path : null) }],
    okLabel: 'Move',
  });
  if (r) await moveItem(path, r.folder);
}

async function renameDoc(path) {
  const r = await appDialog({
    title: 'Rename doc',
    fields: [{ name: 'title', label: 'Title', value: findItem(path)?.title || '' }],
    okLabel: 'Rename',
  });
  if (!r || !r.title) return;
  try {
    // A doc's title is its first "# heading", so rewrite that line...
    const content = docHooks.openContent(path) ?? (await api('/api/docs/' + path)).content;
    const updated = /^#\s+.*$/m.test(content)
      ? content.replace(/^#\s+.*$/m, '# ' + r.title)
      : `# ${r.title}\n\n${content}`;
    await api('/admin/api/docs/' + path, jsonOpts('PUT', { content: updated }));
    docHooks.contentReplaced(path, updated);
    // ...and rename the file to match, if that name is free.
    const dst = join(parentOf(path), fileSlug(r.title));
    if (dst !== path) {
      try {
        const res = await api('/admin/api/move', jsonOpts('POST', { src: path, dst }));
        docHooks.afterMove(path, res.path);
      } catch { /* another doc has that file name; keep the old one */ }
    }
    await refreshTree();
  } catch (err) { toast(err.message); }
}

async function renameFolder(path) {
  const r = await appDialog({
    title: 'Rename folder',
    fields: [{ name: 'name', label: 'Name', value: baseName(path) }],
    okLabel: 'Rename',
  });
  const name = r && cleanFolderName(r.name);
  if (!name || name === baseName(path)) return;
  try {
    await docHooks.beforeChange(path);
    const res = await api('/admin/api/move', jsonOpts('POST', { src: path, dst: join(parentOf(path), name) }));
    docHooks.afterMove(path, res.path);
    await refreshTree();
  } catch (err) { toast(err.message); }
}

async function deleteDoc(path) {
  const title = findItem(path)?.title || path;
  if (!await askConfirm(`Delete "${title}"?`, "This can't be undone.", { okLabel: 'Delete', danger: true })) return;
  try {
    await api('/admin/api/docs/' + path, { method: 'DELETE' });
    docHooks.afterDelete(path);
    await refreshTree();
  } catch (err) { toast(err.message); }
}

async function deleteFolder(path) {
  if (!await askConfirm(`Delete folder "${prettyName(baseName(path))}"?`, '', { okLabel: 'Delete', danger: true })) return;
  try { await api('/admin/api/folders/' + path, { method: 'DELETE' }); await refreshTree(); }
  catch (err) { toast(err.message); }
}

function docMenu(anchor, path) {
  showMenu(anchor, [
    { label: 'Open in editor', action: () => { location.href = editorUrl(path); } },
    { label: 'View on site', action: () => window.open('/docs/' + path, '_blank') },
    'sep',
    { label: 'Rename', action: () => renameDoc(path) },
    { label: 'Move to…', action: () => moveDialog(path, 'doc') },
    'sep',
    { label: 'Delete', danger: true, action: () => deleteDoc(path) },
  ]);
}

function folderMenu(anchor, path) {
  const item = findItem(path);
  const empty = !item || !item.children.length;
  showMenu(anchor, [
    { label: 'New doc in this folder', action: () => newDoc(path) },
    { label: 'New folder inside', action: () => newFolder(path) },
    'sep',
    { label: 'Rename', action: () => renameFolder(path) },
    { label: 'Move to…', action: () => moveDialog(path, 'folder') },
    'sep',
    { label: 'Delete folder', danger: true, disabled: !empty,
      hint: empty ? '' : 'Move or delete what’s inside first', action: () => deleteFolder(path) },
  ]);
}

/** Attach "⋯" menus, empty-folder hints and drag & drop to a tree container. */
function setupTree(el) {
  treeEl = el;

  el.addEventListener('click', e => {
    const more = e.target.closest('[data-more]');
    const newIn = e.target.closest('[data-new-in]');
    if (!more && !newIn) return;
    e.preventDefault(); e.stopPropagation();
    if (newIn) return newDoc(newIn.dataset.newIn);
    const row = more.closest('.row');
    row.dataset.folder !== undefined ? folderMenu(more, row.dataset.folder) : docMenu(more, row.dataset.path);
  });

  // Drag a doc or folder onto a folder to move it; drop on empty space for top level.
  let dragged = null;
  const dropFolderFor = target => {
    const row = target.closest('.row');
    if (!row) return el.contains(target) ? '' : null;
    return row.dataset.folder !== undefined ? row.dataset.folder : parentOf(row.dataset.path);
  };
  const canDrop = folder => folder !== null && dragged && folder !== parentOf(dragged)
    && folder !== dragged && !folder.startsWith(dragged + '/');
  const clearMarks = () => {
    el.querySelectorAll('.drop-target').forEach(n => n.classList.remove('drop-target'));
    el.classList.remove('drop-root');
  };
  el.addEventListener('dragstart', e => {
    const row = e.target.closest('.row[draggable="true"]'); if (!row) return;
    dragged = row.dataset.folder ?? row.dataset.path;
    row.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', dragged);
  });
  el.addEventListener('dragend', () => {
    el.querySelectorAll('.dragging').forEach(n => n.classList.remove('dragging'));
    clearMarks(); dragged = null;
  });
  el.addEventListener('dragover', e => {
    if (!dragged) return;
    const folder = dropFolderFor(e.target);
    clearMarks();
    if (!canDrop(folder)) return;
    e.preventDefault();
    if (folder === '') el.classList.add('drop-root');
    else el.querySelector(`.folder-row[data-folder="${CSS.escape(folder)}"]`)?.classList.add('drop-target');
  });
  el.addEventListener('drop', e => {
    const folder = dropFolderFor(e.target), src = dragged;
    clearMarks();
    if (!canDrop(folder)) return;
    e.preventDefault();
    moveItem(src, folder);
  });
}

// ---------- uploads ----------

async function uploadFile(file) {
  const fd = new FormData();
  fd.append('file', file, file.name || 'pasted.png');
  return api('/admin/api/uploads', { method: 'POST', body: fd });
}

const displayName = n => n.replace(/-[0-9a-f]{8}(\.\w+)$/, '$1');
const altText = n => displayName(n).replace(/\.\w+$/, '');
const markdownFor = it => it.is_image ? `![${altText(it.name)}](${it.url})` : `[${altText(it.name)}](${it.url})`;
const usedCount = it => it.used_in.length + (it.on_home ? 1 : 0);
const formatSize = b => b < 1024 ? b + ' B' : b < 1048576 ? (b / 1024).toFixed(0) + ' KB' : (b / 1048576).toFixed(1) + ' MB';

/**
 * Uploads library grid.
 *   grid, search: elements
 *   onInsert(item): if given, cards get an "Insert" button and clicking a thumbnail inserts
 *   dropZone: element that accepts dropped files
 *   onRename(oldUrl, newUrl, result): a file was renamed (docs on disk are already updated)
 *   onLoad(items): called after the list loads
 */
function createLibrary({ grid, search, onInsert = null, dropZone = null, docLink = editorUrl, onLoad = () => { }, onRename = () => { } }) {
  let items = [];

  async function load() {
    try { items = await api('/admin/api/uploads'); render(); onLoad(items); }
    catch (err) { toast(err.message); }
  }

  function render() {
    const q = search.value.trim().toLowerCase();
    const shown = items.filter(it => it.name.toLowerCase().includes(q));
    if (!shown.length) {
      grid.innerHTML = `<div class="lib-empty">${items.length ? 'No matches.' : 'Nothing uploaded yet. Paste or drop an image into a doc, or click Upload.'}</div>`;
      return;
    }
    grid.innerHTML = shown.map(it => {
      const used = usedCount(it);
      return `<div class="lib-card" data-name="${escapeHtml(it.name)}">
        <button class="lib-thumb" data-act="${onInsert ? 'insert' : 'open'}" title="${onInsert ? 'Insert into doc' : 'Open file'}">
          ${it.is_image ? `<img src="${escapeHtml(it.url)}" alt="" loading="lazy">` : escapeHtml(it.name.split('.').pop().toUpperCase())}
        </button>
        <div class="lib-info">
          <div class="lib-name" title="${escapeHtml(displayName(it.name))}">${escapeHtml(displayName(it.name))}</div>
          <div class="lib-meta ${used ? '' : 'unused'}">${formatSize(it.size)} · ${used ? `used in ${used} place${used > 1 ? 's' : ''}` : 'not used'}</div>
          <div class="lib-used">${it.on_home ? '<a href="/admin/#home">Home page</a>' : ''}${it.used_in.map(p => `<a href="${escapeHtml(docLink(p))}">${escapeHtml(p)}</a>`).join('')}</div>
        </div>
        <div class="lib-actions">
          ${onInsert ? '<button class="insert primary" data-act="insert">Insert</button>' : ''}
          <button data-act="copy" title="Copy markdown">${icon('copy')}Copy</button>
          <button data-act="rename" class="icon-btn" title="Rename file" aria-label="Rename">${icon('edit')}</button>
          <button data-act="delete" class="danger icon-btn" title="Delete file" aria-label="Delete">${icon('trash')}</button>
        </div>
      </div>`;
    }).join('');
  }

  grid.addEventListener('click', async e => {
    const el = e.target.closest('[data-act]'); if (!el) return;
    const item = items.find(it => it.name === el.closest('.lib-card').dataset.name);
    const act = el.dataset.act;
    if (act === 'open') window.open(item.url, '_blank');
    if (act === 'insert') onInsert(item);
    if (act === 'copy') {
      try { await navigator.clipboard.writeText(markdownFor(item)); toast('Copied. Paste it into any doc.'); }
      catch { await askText('Copy this markdown', markdownFor(item), { okLabel: 'Done' }); }
    }
    if (act === 'rename') {
      const used = usedCount(item);
      const vals = await appDialog({
        title: 'Rename file',
        message: used ? `Links in ${used} place${used > 1 ? 's' : ''} will be updated automatically.` : '',
        fields: [{ name: 'name', label: 'Name', value: altText(item.name) }],
        okLabel: 'Rename',
      });
      const name = vals?.name?.trim();
      if (!name || name === altText(item.name)) return;
      try {
        const res = await api('/admin/api/uploads/' + encodeURIComponent(item.name) + '/rename', jsonOpts('POST', { name }));
        onRename(item.url, res.url, res);
        toast(res.docs.length ? `Renamed. Updated ${res.docs.length} doc${res.docs.length > 1 ? 's' : ''}.` : 'Renamed.');
        await load();
      } catch (err) { toast(err.message); }
    }
    if (act === 'delete') {
      const places = [...(item.on_home ? ['the home page'] : []), ...item.used_in];
      const msg = places.length
        ? `It's used in: ${places.join(', ')}. Those pages will show a broken image.`
        : 'No docs use this file.';
      if (!await askConfirm(`Delete ${displayName(item.name)}?`, msg, { okLabel: 'Delete', danger: true })) return;
      try { await api('/admin/api/uploads/' + encodeURIComponent(item.name), { method: 'DELETE' }); await load(); }
      catch (err) { toast(err.message); }
    }
  });
  search.addEventListener('input', render);

  async function upload(files) {
    for (const f of files) {
      try { await uploadFile(f); } catch (err) { toast(`Upload failed: ${err.message}`); }
    }
    await load();
  }

  if (dropZone) {
    dropZone.addEventListener('dragover', e => {
      if (e.dataTransfer.types.includes('Files')) { e.preventDefault(); dropZone.classList.add('drag-over'); }
    });
    dropZone.addEventListener('dragleave', e => { if (!dropZone.contains(e.relatedTarget)) dropZone.classList.remove('drag-over'); });
    dropZone.addEventListener('drop', e => {
      dropZone.classList.remove('drag-over');
      if (!e.dataTransfer.files.length) return;
      e.preventDefault(); upload([...e.dataTransfer.files]);
    });
  }

  return { load, render, upload };
}
