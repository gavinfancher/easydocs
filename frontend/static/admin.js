// Shared by the admin panel (/admin/) and the editor (/admin/edit):
// doc/folder management (dialogs, "⋯" menus, drag & drop) and the uploads library.

const $ = id => document.getElementById(id);

const baseName = p => p.split('/').pop();
const parentOf = p => p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : '';
const join = (folder, name) => folder ? folder + '/' + name : name;
const fileSlug = title => slugify(title).replace(/_/g, '-').replace(/-+/g, '-').replace(/^-|-$/g, '') || 'untitled';
const cleanFolderName = n => n.replace(/[\/\\]+/g, ' ').trim();
const editorUrl = path => '/admin/edit/' + path.split('/').map(encodeURIComponent).join('/');

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
 *   open(path)              -> open a doc in the editor
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
  open: path => { location.href = editorUrl(path); },
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
  const out = [{ value: '', label: 'Top level', icon: 'home' }];
  const walk = (items, depth) => {
    for (const it of items) {
      if (it.type !== 'folder' || it.path === exclude) continue;
      out.push({ value: it.path, label: prettyName(it.name), depth });
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
      { name: 'folder', label: 'Folder', type: 'folder', options: folderOptions(), value: folder ?? docHooks.defaultFolder() },
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
      { name: 'parent', label: 'Inside', type: 'folder', options: folderOptions(), value: parent ?? docHooks.defaultFolder() },
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
    fields: [{ name: 'folder', label: 'Move to', type: 'folder', value: parentOf(path),
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

const siteUrl = path => location.origin + '/docs/' + path.split('/').map(encodeURIComponent).join('/');

const copyLink = path => copyText(siteUrl(path), 'Link copied');

function docMenu(anchor, path, at) {
  const isOpen = docHooks.active() === path;
  showMenu(anchor, [
    { label: isOpen ? 'Open (already open)' : 'Open in editor', icon: 'edit', disabled: isOpen, action: () => docHooks.open(path) },
    { label: 'View on site', icon: 'external', action: () => window.open('/docs/' + path, '_blank') },
    { label: 'Copy link', icon: 'link', action: () => copyLink(path) },
    'sep',
    { label: 'Rename…', icon: 'edit', action: () => renameDoc(path) },
    { label: 'Move to…', icon: 'move', action: () => moveDialog(path, 'doc') },
    { label: 'New doc next to this', icon: 'docPlus', action: () => newDoc(parentOf(path)) },
    'sep',
    { label: 'Delete', icon: 'trash', danger: true, action: () => deleteDoc(path) },
  ], at);
}

function folderMenu(anchor, path, at) {
  const item = findItem(path);
  const empty = !item || !item.children.length;
  showMenu(anchor, [
    { label: 'New doc here', icon: 'docPlus', action: () => newDoc(path) },
    { label: 'New folder here', icon: 'folderPlus', action: () => newFolder(path) },
    'sep',
    { label: 'Rename…', icon: 'edit', action: () => renameFolder(path) },
    { label: 'Move to…', icon: 'move', action: () => moveDialog(path, 'folder') },
    'sep',
    { label: 'Delete folder', icon: 'trash', danger: true, disabled: !empty,
      hint: empty ? '' : 'Move or delete what’s inside first', action: () => deleteFolder(path) },
  ], at);
}

/** "New" button, or right-click on empty space. `folder` undefined = the dialog picks a sensible default. */
function newMenu(anchor, at = null, folder) {
  showMenu(anchor, [
    { label: 'New doc', icon: 'docPlus', action: () => newDoc(folder) },
    { label: 'New folder', icon: 'folderPlus', action: () => newFolder(folder) },
  ], at);
}

/** Attach "⋯" menus, empty-folder hints and drag & drop to a tree container. */
function setupTree(el) {
  treeEl = el;

  el.addEventListener('click', e => {
    const more = e.target.closest('[data-more]');
    const add = e.target.closest('[data-add]');
    const newIn = e.target.closest('[data-new-in]');
    if (!more && !add && !newIn) return;
    e.preventDefault(); e.stopPropagation();
    if (newIn) return newDoc(newIn.dataset.newIn);
    const row = (more || add).closest('.row');
    if (add) return newDoc(row.dataset.folder);
    row.dataset.folder !== undefined ? folderMenu(more, row.dataset.folder) : docMenu(more, row.dataset.path);
  });

  // Right-click anywhere in the tree for the app's own menu instead of the browser's.
  el.addEventListener('contextmenu', e => {
    if (e.shiftKey) return;  // Shift+right-click still gets the browser menu
    e.preventDefault();
    const row = e.target.closest('.row');
    // Opened from the keyboard (menu key): put it under the row rather than at 0,0.
    const at = e.clientX || e.clientY ? { x: e.clientX, y: e.clientY } : null;
    if (!row) return newMenu(el, at, '');
    row.dataset.folder !== undefined ? folderMenu(row, row.dataset.folder, at) : docMenu(row, row.dataset.path, at);
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

/** Upload a file: the API hands out a presigned URL and the browser sends the file straight to it (R2). */
async function uploadFile(file, folder = '') {
  const filename = file.name || 'pasted.png';
  const started = await api('/admin/api/uploads/start',
    jsonOpts('POST', { filename, content_type: file.type, size: file.size }));
  const { method, url, headers } = started.upload;
  // A presigned R2 URL, or (without R2) a path on the API, which needs the token.
  const local = url.startsWith('/');
  let res;
  try { res = await fetch(local ? API_BASE + url : url, { method, headers: local ? auth.headers(headers) : headers, body: file }); }
  catch { throw new Error('Couldn’t reach file storage'); }
  if (!res.ok) throw new Error(`File storage said ${res.status}`);
  const done = await api(`/admin/api/uploads/${encodeURIComponent(started.file)}/finish`, jsonOpts('POST', { folder }));
  return { ...done, name: started.name };
}

const displayName = n => n.replace(/-[0-9a-f]{8}(\.\w+)$/, '$1');
const altText = n => displayName(n).replace(/\.\w+$/, '');
const markdownFor = it => it.is_image ? `![${altText(it.name)}](${it.url})` : `[${altText(it.name)}](${it.url})`;
const usedCount = it => it.used_in.length + (it.on_home ? 1 : 0);
const formatSize = b => b < 1024 ? b + ' B' : b < 1048576 ? (b / 1024).toFixed(0) + ' KB' : (b / 1048576).toFixed(1) + ' MB';

// Upload folders are labels on the server (see main.py), so moving a file never changes its URL.
const ALL = null;  // "All uploads" view; '' is the "Unsorted" view (files in no folder)

/** Sort folder paths so each folder comes right after its parent. */
const byFolderPath = (a, b) => {
  const pa = a.split('/'), pb = b.split('/');
  for (let i = 0; i < Math.min(pa.length, pb.length); i++) {
    const c = pa[i].localeCompare(pb[i], undefined, { sensitivity: 'base', numeric: true });
    if (c) return c;
  }
  return pa.length - pb.length;
};

function uploadFolderOptions(folders, exclude = null) {
  return [{ value: '', label: 'Unsorted', icon: 'library' },
    ...folders.filter(f => !exclude || (f !== exclude && !f.startsWith(exclude + '/')))
      .map(f => ({ value: f, label: baseName(f), depth: f.split('/').length }))];
}

/**
 * Uploads library: a folder list beside a grid of files.
 *   grid, search, folderNav: elements
 *   onInsert(item): if given, cards get an "Insert" button and clicking a thumbnail inserts
 *   dropZone: element that accepts dropped files (uploaded into the open folder)
 *   onRename(oldUrl, newUrl, result): a file was renamed (docs on disk are already updated)
 *   onLoad(items): called after the list loads
 *   storageKey: remembers the open folder between visits
 */
function createLibrary({ grid, search, folderNav, onInsert = null, dropZone = null, docLink = editorUrl,
                         onLoad = () => { }, onRename = () => { }, storageKey = 'easydocs.uploadFolder' }) {
  let items = [], folders = [];
  let cur = (() => { try { const v = localStorage.getItem(storageKey); return v === null ? ALL : JSON.parse(v); } catch { return ALL; } })();
  const setCur = f => { cur = f; try { localStorage.setItem(storageKey, JSON.stringify(f)); } catch { } render(); };
  const uploadTarget = () => cur ?? '';

  // Placeholder cards until the first list arrives, so the grid doesn't sit empty.
  const SKELETON = '<div class="lib-card skeleton" aria-hidden="true"><div class="lib-thumb"></div>'
    + '<div class="lib-info"><div class="sk-line"></div><div class="sk-line short"></div></div></div>';
  let loaded = false;

  async function load() {
    if (!loaded) grid.innerHTML = SKELETON.repeat(8);
    try {
      const res = await api('/admin/api/uploads');
      loaded = true;
      items = res.files; folders = res.folders.sort(byFolderPath);
      if (cur && !folders.includes(cur)) cur = ALL;
      render(); onLoad(items);
    } catch (err) { toast(err.message); }
  }

  // ---------- folder list ----------
  function renderFolders() {
    if (!folderNav) return;
    const count = f => items.filter(it => it.folder === f).length;
    const row = (value, label, ico, depth, n) => `<button class="lib-folder ${value === cur ? 'active' : ''} ${ico === 'folder' ? 'is-folder' : ''}"
        data-folder="${escapeHtml(JSON.stringify(value))}" style="--depth:${depth}" title="${escapeHtml(label)}">
        ${icon(ico)}<span class="label-text">${escapeHtml(label)}</span><span class="count">${n || ''}</span></button>`;
    folderNav.innerHTML =
      row(ALL, 'All uploads', 'library', 0, items.length) +
      row('', 'Unsorted', 'image', 0, count('')) +
      `<div class="lib-folders-head"><span>Folders</span>
        <button class="icon-btn" data-new-folder title="New folder" aria-label="New folder">${icon('folderPlus')}</button></div>` +
      (folders.length
        ? folders.map(f => row(f, baseName(f), 'folder', f.split('/').length - 1, count(f))).join('')
        : '<div class="lib-folders-empty">No folders yet. Right-click here or use + to make one.</div>');
  }
  const folderOf = el => { const b = el.closest('[data-folder]'); return b ? JSON.parse(b.dataset.folder) : undefined; };

  // ---------- grid ----------
  /** One line per card, so every card is the same height: the first place it's used, then "+N" for the rest. */
  function usedLine(it) {
    const places = [...(it.on_home ? [{ href: '/admin/home', label: 'Home page' }] : []),
                    ...it.used_in.map(p => ({ href: docLink(p), label: p }))];
    if (!places.length) return '<div class="lib-used unused">Not used</div>';
    const [first, ...rest] = places;
    return `<div class="lib-used" title="${escapeHtml(places.map(p => p.label).join('\n'))}">`
      + `<a href="${escapeHtml(first.href)}">${escapeHtml(first.label)}</a>`
      + (rest.length ? `<span class="more">+${rest.length} more</span>` : '') + '</div>';
  }

  function render() {
    renderFolders();
    const q = search.value.trim().toLowerCase();
    // Searching looks through every folder; otherwise show the open folder.
    const shown = items.filter(it => q ? it.name.toLowerCase().includes(q) : cur === ALL || it.folder === cur);
    if (!shown.length) {
      grid.innerHTML = `<div class="lib-empty">${
        q ? 'No matches.'
        : !items.length ? 'Nothing uploaded yet. Paste or drop an image into a doc, or click Upload.'
        : cur === '' ? 'Everything is filed away in folders.'
        : 'This folder is empty.<br>Drag uploads onto it in the list, or drop files here to upload them into it.'}</div>`;
      return;
    }
    const showFolder = q || cur === ALL;
    grid.innerHTML = shown.map(it => {
      const used = usedCount(it);
      return `<div class="lib-card" data-name="${escapeHtml(it.name)}" draggable="true">
        <button class="lib-thumb" data-act="${onInsert ? 'insert' : 'open'}" title="${onInsert ? 'Insert into doc' : 'Open file'}" draggable="false">
          ${it.is_image ? `<img data-upload="${escapeHtml(it.url)}" alt="" loading="lazy" draggable="false">` : escapeHtml(it.name.split('.').pop().toUpperCase())}
        </button>
        <div class="lib-info">
          <div class="lib-name" title="${escapeHtml(displayName(it.name))}">${escapeHtml(displayName(it.name))}</div>
          <div class="lib-meta">${formatSize(it.size)}</div>
          ${showFolder ? `<div class="lib-in">${icon('folder')}${escapeHtml(it.folder || 'Unsorted')}</div>` : ''}
          ${usedLine(it)}
        </div>
        <div class="lib-actions">
          ${onInsert ? '<button class="insert primary" data-act="insert">Insert</button>'
                     : `<button class="insert" data-act="copy" title="Copy markdown">${icon('copy')}Copy</button>`}
          <button data-act="menu" class="icon-btn" title="More actions (or right-click)" aria-label="More actions">${icon('dots')}</button>
        </div>
      </div>`;
    }).join('');
    resolveUploads(grid);
  }

  // ---------- file actions ----------
  const itemFor = el => items.find(it => it.name === el.closest('.lib-card')?.dataset.name);

  function fileMenu(item, anchor, at) {
    showMenu(anchor, [
      ...(onInsert ? [{ label: 'Insert into doc', icon: 'plus', action: () => onInsert(item) }] : []),
      { label: 'Copy markdown', icon: 'copy', action: () => copyMarkdown(item) },
      { label: 'Open file', icon: 'external', action: () => openUpload(item.url) },
      'sep',
      { label: 'Rename…', icon: 'edit', action: () => renameFile(item) },
      { label: 'Move to folder…', icon: 'move', action: () => moveFile(item) },
      'sep',
      { label: 'Delete', icon: 'trash', danger: true, action: () => deleteFile(item) },
    ], at);
  }

  async function copyMarkdown(item) {
    try { await navigator.clipboard.writeText(markdownFor(item)); toast('Copied. Paste it into any doc.'); }
    catch { await askText('Copy this markdown', markdownFor(item), { okLabel: 'Done' }); }
  }

  async function renameFile(item) {
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

  async function moveFiles(names, folder) {
    try {
      await api('/admin/api/uploads/move', jsonOpts('POST', { names, folder }));
      toast(`Moved to ${folder ? baseName(folder) : 'Unsorted'}`);
      await load();
    } catch (err) { toast(err.message); }
  }

  async function moveFile(item) {
    const r = await appDialog({
      title: `Move ${displayName(item.name)}`,
      message: 'Links to it keep working. Folders only organize your uploads.',
      fields: [{ name: 'folder', label: 'Move to', type: 'folder', value: item.folder, options: uploadFolderOptions(folders) }],
      okLabel: 'Move',
    });
    if (r && r.folder !== item.folder) await moveFiles([item.name], r.folder);
  }

  async function deleteFile(item) {
    const places = [...(item.on_home ? ['the home page'] : []), ...item.used_in];
    const msg = places.length
      ? `It's used in: ${places.join(', ')}. Those pages will show a broken image.`
      : 'No docs use this file.';
    if (!await askConfirm(`Delete ${displayName(item.name)}?`, msg, { okLabel: 'Delete', danger: true })) return;
    try { await api('/admin/api/uploads/' + encodeURIComponent(item.name), { method: 'DELETE' }); await load(); }
    catch (err) { toast(err.message); }
  }

  grid.addEventListener('click', async e => {
    const el = e.target.closest('[data-act]'); if (!el) return;
    const item = itemFor(el); if (!item) return;
    const act = el.dataset.act;
    if (act === 'open') openUpload(item.url);
    if (act === 'insert') onInsert(item);
    if (act === 'copy') copyMarkdown(item);
    if (act === 'menu') fileMenu(item, el);
  });
  grid.addEventListener('contextmenu', e => {
    const item = itemFor(e.target);
    if (!item || e.shiftKey) return;
    e.preventDefault();
    fileMenu(item, e.target.closest('.lib-card'), e.clientX || e.clientY ? { x: e.clientX, y: e.clientY } : null);
  });

  // ---------- folder actions ----------
  async function newFolder(parent = uploadTarget()) {
    const r = await appDialog({
      title: 'New folder',
      fields: [
        { name: 'name', label: 'Name', placeholder: 'e.g. Screenshots' },
        { name: 'parent', label: 'Inside', type: 'folder', value: parent,
          options: uploadFolderOptions(folders).map(o => o.value ? o : { ...o, label: 'Top level', icon: 'home' }) },
      ],
      okLabel: 'Create folder',
    });
    const name = r && cleanFolderName(r.name);
    if (!name) return;
    try {
      const res = await api('/admin/api/upload-folders', jsonOpts('POST', { path: join(r.parent, name) }));
      folders.push(res.path); setCur(res.path);
      await load();
    } catch (err) { toast(err.message); }
  }

  async function renameFolder(path) {
    const r = await appDialog({ title: 'Rename folder', fields: [{ name: 'name', label: 'Name', value: baseName(path) }], okLabel: 'Rename' });
    const name = r && cleanFolderName(r.name);
    if (!name || name === baseName(path)) return;
    await moveFolder(path, join(parentOf(path), name));
  }

  async function moveFolder(src, dst) {
    if (src === dst) return;
    try {
      const res = await api('/admin/api/upload-folders/move', jsonOpts('POST', { src, dst }));
      if (cur === src || cur?.startsWith(src + '/')) cur = res.path + cur.slice(src.length);
      await load();
    } catch (err) { toast(err.message); }
  }

  async function moveFolderDialog(path) {
    const r = await appDialog({
      title: `Move folder ${baseName(path)}`,
      fields: [{ name: 'parent', label: 'Move into', type: 'folder', value: parentOf(path),
                 options: uploadFolderOptions(folders, path).map(o => o.value ? o : { ...o, label: 'Top level', icon: 'home' }) }],
      okLabel: 'Move',
    });
    if (r) await moveFolder(path, join(r.parent, baseName(path)));
  }

  async function deleteFolder(path) {
    const n = items.filter(it => it.folder === path || it.folder.startsWith(path + '/')).length;
    const dest = parentOf(path) ? `“${baseName(parentOf(path))}”` : 'Unsorted';
    const msg = n ? `Its ${n} file${n > 1 ? 's' : ''} and any subfolders move to ${dest}. No files are deleted.` : 'It’s empty.';
    if (!await askConfirm(`Remove folder “${baseName(path)}”?`, msg, { okLabel: 'Remove folder', danger: true })) return;
    try {
      await api('/admin/api/upload-folders/delete', jsonOpts('POST', { path }));
      if (cur === path) cur = parentOf(path) || ALL;
      await load();
    } catch (err) { toast(err.message); }
  }

  function folderMenu(path, anchor, at) {
    if (path === ALL || path === '') {
      return showMenu(anchor, [{ label: 'New folder', icon: 'folderPlus', action: () => newFolder('') }], at);
    }
    showMenu(anchor, [
      { label: 'New folder inside', icon: 'folderPlus', action: () => newFolder(path) },
      'sep',
      { label: 'Rename…', icon: 'edit', action: () => renameFolder(path) },
      { label: 'Move to…', icon: 'move', action: () => moveFolderDialog(path) },
      'sep',
      { label: 'Remove folder', icon: 'trash', danger: true, hint: 'Files inside are kept', action: () => deleteFolder(path) },
    ], at);
  }

  if (folderNav) {
    folderNav.addEventListener('click', e => {
      if (e.target.closest('[data-new-folder]')) return newFolder();
      const f = folderOf(e.target);
      if (f !== undefined) { search.value = ''; setCur(f); }
    });
    folderNav.addEventListener('contextmenu', e => {
      if (e.shiftKey) return;
      e.preventDefault();
      const f = folderOf(e.target);
      const at = e.clientX || e.clientY ? { x: e.clientX, y: e.clientY } : null;
      folderMenu(f === undefined ? '' : f, e.target.closest('[data-folder]') || folderNav, at);
    });

    // Drag cards onto a folder to file them; drop files from your computer onto a folder to upload there.
    const target = e => { const f = folderOf(e.target); return f === undefined || f === ALL ? undefined : f; };
    const clear = () => folderNav.querySelectorAll('.drop-target').forEach(n => n.classList.remove('drop-target'));
    folderNav.addEventListener('dragover', e => {
      const f = target(e);
      clear();
      if (f === undefined) return;
      if (!e.dataTransfer.types.includes('application/x-easydocs-upload') && !e.dataTransfer.types.includes('Files')) return;
      e.preventDefault(); e.stopPropagation();
      e.target.closest('[data-folder]').classList.add('drop-target');
    });
    folderNav.addEventListener('dragleave', e => { if (!folderNav.contains(e.relatedTarget)) clear(); });
    folderNav.addEventListener('drop', e => {
      const f = target(e);
      clear();
      if (f === undefined) return;
      e.preventDefault(); e.stopPropagation();
      dropZone?.classList.remove('drag-over');
      const name = e.dataTransfer.getData('application/x-easydocs-upload');
      if (name) { if (items.find(it => it.name === name)?.folder !== f) moveFiles([name], f); }
      else if (e.dataTransfer.files.length) upload([...e.dataTransfer.files], f);
    });
  }
  grid.addEventListener('dragstart', e => {
    const card = e.target.closest('.lib-card'); if (!card) return;
    e.dataTransfer.setData('application/x-easydocs-upload', card.dataset.name);
    e.dataTransfer.effectAllowed = 'move';
    card.classList.add('dragging');
  });
  grid.addEventListener('dragend', () => grid.querySelectorAll('.dragging').forEach(n => n.classList.remove('dragging')));

  search.addEventListener('input', render);

  async function upload(files, folder = uploadTarget()) {
    for (const f of files) {
      try { await uploadFile(f, folder); } catch (err) { toast(`Upload failed: ${err.message}`); }
    }
    await load();
  }

  if (dropZone) {
    // Only files from the computer; dragging a card around shouldn't light this up.
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

  return { load, render, upload, newFolder };
}
