// Shared helpers for the viewer and admin pages.

const isDark = () => window.matchMedia('(prefers-color-scheme: dark)').matches;

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function slugify(s) {
  return s.toLowerCase().trim().replace(/[^\w\s-]/g, '').replace(/\s+/g, '-');
}

function prettyName(s) {
  const t = s.replace(/^\d+[-_ ]/, '').replace(/[-_]+/g, ' ').trim();
  return t.charAt(0).toUpperCase() + t.slice(1);
}

// ---------- markdown ----------

const renderer = new marked.Renderer();
renderer.code = function (a, b) {
  // marked passes (text, lang) in older versions and a token object in newer ones.
  const text = typeof a === 'object' ? a.text : a;
  const lang = ((typeof a === 'object' ? a.lang : b) || '').trim().split(/\s+/)[0];
  if (lang === 'mermaid') return `<pre class="mermaid">${escapeHtml(text)}</pre>`;
  const html = lang && hljs.getLanguage(lang)
    ? hljs.highlight(text, { language: lang }).value
    : escapeHtml(text);
  return `<pre><code class="hljs language-${escapeHtml(lang)}">${html}</code></pre>`;
};
// Image size goes after a "|" in the alt text: ![Diagram|50%](/uploads/x.png) or ![Logo|120](...) for pixels.
const IMG_SIZE_RE = /\|\s*(\d{1,4})\s*(%|px)?\s*$/;
function splitImageSize(alt) {
  const m = alt.match(IMG_SIZE_RE);
  if (!m) return { alt, width: '' };
  const n = Number(m[1]), pct = m[2] === '%';
  return { alt: alt.slice(0, m.index).trimEnd(), width: pct ? Math.min(n, 100) + '%' : n + 'px' };
}
const unescapeHtml = s => String(s).replace(/&(amp|lt|gt|quot|#39);/g, (_, e) => ({ amp: '&', lt: '<', gt: '>', quot: '"', '#39': "'" }[e]));
renderer.image = function (a, b, c) {
  // marked passes (href, title, text) in older versions and a token object in newer ones.
  const tok = typeof a === 'object' ? a : { href: a, title: b, text: c };
  const { alt, width } = splitImageSize(unescapeHtml(tok.text || ''));
  let src;
  try { src = encodeURI(tok.href || '').replace(/%25/g, '%'); } catch { return escapeHtml(alt); }
  return `<img src="${escapeHtml(src)}" alt="${escapeHtml(alt)}"` +
    (tok.title ? ` title="${escapeHtml(tok.title)}"` : '') + (width ? ` style="width:${width}"` : '') + '>';
};
marked.setOptions({ renderer, gfm: true, breaks: false });

mermaid.initialize({ startOnLoad: false, theme: isDark() ? 'dark' : 'default', securityLevel: 'strict' });

let mermaidSeq = 0;
async function renderMarkdown(src, el) {
  el.innerHTML = DOMPurify.sanitize(marked.parse(src));
  const seen = {};
  el.querySelectorAll('h1, h2, h3, h4').forEach(h => {
    let id = slugify(h.textContent) || 'section';
    if (seen[id]) id += '-' + seen[id]++; else seen[id] = 1;
    h.id = id;
  });
  el.querySelectorAll('a[href^="http"]').forEach(a => { a.target = '_blank'; a.rel = 'noopener'; });
  const nodes = [...el.querySelectorAll('pre.mermaid')];
  if (nodes.length) {
    const run = ++mermaidSeq;
    try { await mermaid.run({ nodes }); }
    catch (e) { if (run === mermaidSeq) console.warn('mermaid:', e); }
  }
}

// ---------- api ----------

async function api(url, opts = {}) {
  const res = await fetch(url, opts);
  if (res.status === 401 && url.startsWith('/admin/api/')) {
    // Session expired or logged out: go log in, then come back here.
    location.href = '/admin/login?next=' + encodeURIComponent(location.pathname) + location.hash;
    throw new Error('Please log in again');
  }
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch { }
    throw new Error(msg);
  }
  return res.json();
}

const jsonOpts = (method, body) => ({
  method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
});

// ---------- sidebar tree ----------

// Remembers which folders are collapsed across page loads.
const collapsed = (() => {
  try { return new Set(JSON.parse(localStorage.getItem('mydocs.collapsed') || '[]')); }
  catch { return new Set(); }
})();
function saveCollapsed() {
  try { localStorage.setItem('mydocs.collapsed', JSON.stringify([...collapsed])); } catch { }
}

const ICONS = {
  chevron: '<svg class="ico chev" viewBox="0 0 16 16" aria-hidden="true"><path d="M6 4l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  folder: '<svg class="ico" viewBox="0 0 16 16" aria-hidden="true"><path d="M2 4.5A1.5 1.5 0 0 1 3.5 3h2.8l1.5 1.5h4.7A1.5 1.5 0 0 1 14 6v5.5a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 2 11.5z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>',
  doc: '<svg class="ico" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 1.8h5l3.2 3.2v9.2H4z M9 1.8V5h3.2 M6 8.2h4 M6 10.7h4" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round" stroke-linecap="round"/></svg>',
  dots: '<svg class="ico" viewBox="0 0 16 16" aria-hidden="true"><circle cx="3.5" cy="8" r="1.3" fill="currentColor"/><circle cx="8" cy="8" r="1.3" fill="currentColor"/><circle cx="12.5" cy="8" r="1.3" fill="currentColor"/></svg>',
};

// Simple line icons (16px, currentColor). Use icon('name') in JS, or data-icon="name" in HTML.
const ICON_PATHS = {
  plus: '<path d="M8 3v10M3 8h10"/>',
  link: '<path d="M6.8 9.2a2.6 2.6 0 0 0 3.7 0l2.4-2.4a2.6 2.6 0 0 0-3.7-3.7l-.7.7M9.2 6.8a2.6 2.6 0 0 0-3.7 0L3.1 9.2a2.6 2.6 0 0 0 3.7 3.7l.7-.7"/>',
  image: '<rect x="2" y="3" width="12" height="10" rx="1.5"/><circle cx="5.8" cy="6.3" r="1.1"/><path d="M2.5 12.3l3.4-3.4 2.4 2.4 1.8-1.8 3.4 3.4"/>',
  library: '<rect x="4.5" y="2" width="9.5" height="9" rx="1.4"/><path d="M2 5v7.6c0 .8.6 1.4 1.4 1.4H11M6.3 9l2-2 1.4 1.4 1-1 1.8 1.8"/>',
  panel: '<rect x="2" y="2.5" width="12" height="11" rx="1.5"/><path d="M6 2.5v11"/>',
  x: '<path d="M4 4l8 8M12 4l-8 8"/>',
  external: '<path d="M9.5 2.5h4v4M13.5 2.5L8 8M11.5 9.5v4h-9v-9h4"/>',
  back: '<path d="M10 3.5L5.5 8l4.5 4.5"/>',
  upload: '<path d="M8 10.5V2.5M4.7 5.8L8 2.5l3.3 3.3M2.5 10.5v3h11v-3"/>',
  logout: '<path d="M6.5 2.5h-4v11h4M10 5l3 3-3 3M13 8H6"/>',
  edit: '<path d="M10.3 2.7l3 3L6 13H3v-3z"/>',
  docs: '<path d="M4 1.8h5l3.2 3.2v9.2H4zM9 1.8V5h3.2M6 8.2h4M6 10.7h4"/>',
  folderPlus: '<path d="M2 4.5A1.5 1.5 0 0 1 3.5 3h2.8l1.5 1.5h4.7A1.5 1.5 0 0 1 14 6v5.5a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 2 11.5zM8 6.7v4M6 8.7h4"/>',
  copy: '<rect x="5" y="5" width="8.5" height="8.5" rx="1.3"/><path d="M11 5V3.3c0-.5-.4-.8-.8-.8H3.3c-.5 0-.8.4-.8.8v6.9c0 .4.4.8.8.8H5"/>',
  trash: '<path d="M2.5 4.5h11M6 4.5V2.8h4v1.7M4 4.5l.7 9h6.6l.7-9"/>',
  home: '<path d="M2.5 7.3L8 2.8l5.5 4.5M4 6.2v7.3h3V10h2v3.5h3V6.2"/>',
  folder: '<path d="M2 4.5A1.5 1.5 0 0 1 3.5 3h2.8l1.5 1.5h4.7A1.5 1.5 0 0 1 14 6v5.5a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 2 11.5z"/>',
  check: '<path d="M3 8.5l3.2 3L13 4.5"/>',
  docPlus: '<path d="M4 1.8h5l3.2 3.2v4M9 1.8V5h3.2M4 1.8v12.4h5M12 11v4M10 13h4"/>',
  move: '<path d="M2 4.5A1.5 1.5 0 0 1 3.5 3h2.8l1.5 1.5h4.7A1.5 1.5 0 0 1 14 6v5.5a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 2 11.5zM6 8.7h4M8.5 7l1.6 1.7-1.6 1.7"/>',
  open: '<path d="M4 1.8h5l3.2 3.2v9.2H4zM9 1.8V5h3.2"/>',
  eye: '<path d="M1.5 8S4 3.5 8 3.5 14.5 8 14.5 8 12 12.5 8 12.5 1.5 8 1.5 8z"/><circle cx="8" cy="8" r="2"/>',
  dots: '<circle cx="3.5" cy="8" r=".6"/><circle cx="8" cy="8" r=".6"/><circle cx="12.5" cy="8" r=".6"/>',
};
const icon = name =>
  `<svg class="ico" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON_PATHS[name]}</svg>`;
document.querySelectorAll('[data-icon]').forEach(el => el.insertAdjacentHTML('afterbegin', icon(el.dataset.icon)));

/** Sidebar toggle: collapses the sidebar on desktop (remembered), slides it in on phones. */
(() => {
  const btn = document.getElementById('sidebar-toggle');
  if (!btn) return;
  try { if (localStorage.getItem('mydocs.sidebar') === 'collapsed') document.body.classList.add('sidebar-collapsed'); } catch { }
  btn.addEventListener('click', () => {
    if (window.matchMedia('(max-width: 800px)').matches) { document.body.classList.toggle('nav-open'); return; }
    const c = document.body.classList.toggle('sidebar-collapsed');
    try { localStorage.setItem('mydocs.sidebar', c ? 'collapsed' : 'open'); } catch { }
  });
})();

const hasDocs = item => item.type === 'doc' || item.children.some(hasDocs);

/**
 * Render the doc tree into `container`.
 * opts.href(path) -> link for a doc
 * opts.active     -> currently open doc path
 * opts.editable   -> admin mode: "⋯" menus, drag & drop, empty-folder hints
 */
function renderTree(container, tree, opts) {
  const moreBtn = '<button class="more" data-more title="More actions (or right-click)" aria-label="More actions">' + ICONS.dots + '</button>';
  const addBtn = '<button class="more" data-add title="New doc in this folder" aria-label="New doc in this folder">' + icon('plus') + '</button>';
  const build = (items) => {
    const ul = document.createElement('ul');
    for (const item of items) {
      if (!opts.editable && !hasDocs(item)) continue;  // readers don't need empty folders
      const li = document.createElement('li');
      if (item.type === 'folder') {
        const details = document.createElement('details');
        details.open = !collapsed.has(item.path);
        details.addEventListener('toggle', () => {
          details.open ? collapsed.delete(item.path) : collapsed.add(item.path);
          saveCollapsed();
        });
        const summary = document.createElement('summary');
        summary.className = 'row folder-row';
        summary.dataset.folder = item.path;
        if (opts.editable) summary.draggable = true;
        summary.innerHTML = ICONS.chevron + ICONS.folder +
          `<span class="label-text">${escapeHtml(prettyName(item.name))}</span>` + (opts.editable ? addBtn + moreBtn : '');
        const children = build(item.children);
        if (opts.editable && !item.children.length) {
          children.innerHTML = `<li class="empty-hint">Empty — <button data-new-in="${escapeHtml(item.path)}">add a doc</button></li>`;
        }
        details.append(summary, children);
        li.append(details);
      } else {
        const row = document.createElement('div');
        row.className = 'row doc-row';
        row.dataset.path = item.path;
        if (opts.editable) row.draggable = true;
        if (item.path === opts.active) row.classList.add('active');
        row.innerHTML = `<a href="${escapeHtml(opts.href(item.path))}" draggable="false">${ICONS.doc}` +
          `<span class="label-text">${escapeHtml(item.title || prettyName(item.name))}</span></a>` + (opts.editable ? moreBtn : '');
        li.append(row);
      }
      ul.append(li);
    }
    return ul;
  };
  container.replaceChildren(build(tree));
}

function setActive(container, path) {
  container.querySelectorAll('[data-path]').forEach(el => el.classList.toggle('active', el.dataset.path === path));
}

// ---------- in-app dialogs (instead of the browser's prompt/confirm) ----------

/** A folder list to pick from (in-app, instead of the browser's dropdown). options: [{value, label, depth, icon}] */
function folderPickerHtml(f) {
  return `<div class="folder-pick" role="listbox" tabindex="0" aria-label="${escapeHtml(f.label || 'Folder')}">` +
    f.options.map(o => `<button type="button" role="option" tabindex="-1" data-value="${escapeHtml(o.value)}" style="--depth:${o.depth || 0}">` +
      `${icon(o.icon || 'folder')}<span class="label-text">${escapeHtml(o.label)}</span>${icon('check')}</button>`).join('') +
    `</div><input type="hidden" name="${f.name}">`;
}

function setupFolderPicker(list, input, onChoose) {
  const opts = () => [...list.querySelectorAll('[role=option]')];
  const select = (btn, scroll = true) => {
    if (!btn) return;
    input.value = btn.dataset.value;
    opts().forEach(o => o.setAttribute('aria-selected', o === btn));
    if (scroll) btn.scrollIntoView({ block: 'nearest' });
  };
  select(opts().find(o => o.dataset.value === input.value) || opts()[0]);
  list.addEventListener('click', e => select(e.target.closest('[role=option]'), false));
  list.addEventListener('dblclick', e => { if (e.target.closest('[role=option]')) onChoose(); });
  list.addEventListener('keydown', e => {
    const all = opts(), i = all.findIndex(o => o.dataset.value === input.value);
    if (e.key === 'ArrowDown') { e.preventDefault(); select(all[Math.min(i + 1, all.length - 1)]); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); select(all[Math.max(i - 1, 0)]); }
    else if (e.key === 'Home') { e.preventDefault(); select(all[0]); }
    else if (e.key === 'End') { e.preventDefault(); select(all[all.length - 1]); }
  });
}

/**
 * Show a dialog. With `fields` it resolves to {name: value} (or null if cancelled);
 * without, it resolves to true/false.
 * field: { name, label, value, type: 'text' | 'folder', options: [{value, label, depth, icon}], placeholder, hint }
 */
function appDialog({ title, message = '', fields = null, okLabel = 'OK', danger = false }) {
  return new Promise(resolve => {
    const dlg = document.createElement('dialog');
    dlg.className = 'app-dialog';
    // A folder list holds several buttons, so it can't sit inside a <label>.
    const fieldHtml = (fields || []).map(f => f.type === 'folder'
      ? `<div class="field">
          ${f.label ? `<span>${escapeHtml(f.label)}</span>` : ''}
          ${folderPickerHtml(f)}
          ${f.hint ? `<small>${escapeHtml(f.hint)}</small>` : ''}
        </div>`
      : `<label class="field">
          ${f.label ? `<span>${escapeHtml(f.label)}</span>` : ''}
          <input name="${f.name}" autocomplete="off" spellcheck="false" placeholder="${escapeHtml(f.placeholder || '')}">
          ${f.hint ? `<small>${escapeHtml(f.hint)}</small>` : ''}
        </label>`).join('');
    dlg.innerHTML = `<form method="dialog">
      <h3>${escapeHtml(title)}</h3>
      ${message ? `<p>${escapeHtml(message)}</p>` : ''}
      ${fieldHtml}
      <div class="dialog-actions">
        <button type="button" data-cancel>Cancel</button>
        <button value="ok" class="${danger ? 'danger-fill' : 'primary'}">${escapeHtml(okLabel)}</button>
      </div>
    </form>`;
    for (const f of fields || []) dlg.querySelector(`[name="${f.name}"]`).value = f.value ?? '';
    dlg.querySelectorAll('.folder-pick').forEach(list =>
      setupFolderPicker(list, list.nextElementSibling, () => dlg.close('ok')));
    dlg.querySelector('[data-cancel]').onclick = () => dlg.close('cancel');
    dlg.addEventListener('close', () => {
      const ok = dlg.returnValue === 'ok';
      const values = {};
      for (const f of fields || []) values[f.name] = dlg.querySelector(`[name="${f.name}"]`).value.trim();
      dlg.remove();
      resolve(fields ? (ok ? values : null) : ok);
    });
    document.body.append(dlg);
    dlg.showModal();
    const first = dlg.querySelector('input:not([type=hidden]), .folder-pick');
    if (first) { first.focus(); if (first.select) first.select(); }
  });
}
const askText = (title, value = '', opts = {}) =>
  appDialog({ title, fields: [{ name: 'v', value }], ...opts }).then(r => r && r.v);
const askConfirm = (title, message = '', opts = {}) => appDialog({ title, message, ...opts });

// ---------- popup menu ----------

let closeOpenMenu = null, openMenuAnchor = null;

/**
 * items: [{ label, action, icon, danger, disabled, hint }] or 'sep'
 * at: {x, y} opens the menu at the pointer (right-click) instead of under `anchor`.
 */
function showMenu(anchor, items, at = null) {
  // Clicking the same button again closes its menu.
  if (closeOpenMenu && openMenuAnchor === anchor && !at) { closeOpenMenu(); return; }
  closeOpenMenu?.();
  const menu = document.createElement('div');
  menu.className = 'popup-menu';
  menu.setAttribute('role', 'menu');
  items.forEach(it => {
    if (it === 'sep') { menu.append(document.createElement('hr')); return; }
    const b = document.createElement('button');
    b.setAttribute('role', 'menuitem');
    b.className = it.danger ? 'danger' : '';
    b.disabled = !!it.disabled;
    b.innerHTML = (it.icon ? icon(it.icon) : '<span class="ico"></span>') +
      `<span class="menu-label">${escapeHtml(it.label)}${it.hint ? `<small>${escapeHtml(it.hint)}</small>` : ''}</span>`;
    b.onclick = () => { close(); it.action(); };
    menu.append(b);
  });
  document.body.append(menu);
  const r = anchor.getBoundingClientRect();
  const w = menu.offsetWidth, h = menu.offsetHeight;
  let left, top;
  if (at) {
    left = Math.min(at.x, window.innerWidth - w - 8);
    top = at.y + h + 8 > window.innerHeight ? at.y - h : at.y;
  } else {
    left = Math.min(r.left, window.innerWidth - w - 8);
    top = r.bottom + h + 8 > window.innerHeight ? r.top - h - 4 : r.bottom + 4;
  }
  Object.assign(menu.style, { left: Math.max(8, left) + 'px', top: Math.max(8, top) + 'px' });
  anchor.classList.add('menu-open');

  const buttons = () => [...menu.querySelectorAll('button:not(:disabled)')];
  const onDown = e => { if (!menu.contains(e.target) && (at || !anchor.contains(e.target))) close(); };
  const onKey = e => {
    if (e.key === 'Escape') { e.preventDefault(); close(); }
    else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const bs = buttons(), i = bs.indexOf(document.activeElement);
      bs[(i + (e.key === 'ArrowDown' ? 1 : -1) + bs.length) % bs.length]?.focus();
    }
  };
  function close() {
    menu.remove(); anchor.classList.remove('menu-open'); closeOpenMenu = openMenuAnchor = null;
    document.removeEventListener('mousedown', onDown); document.removeEventListener('keydown', onKey);
    window.removeEventListener('blur', close); window.removeEventListener('resize', close);
    document.removeEventListener('scroll', close, true);
  }
  closeOpenMenu = close; openMenuAnchor = anchor;
  setTimeout(() => {
    document.addEventListener('mousedown', onDown); document.addEventListener('keydown', onKey);
    window.addEventListener('blur', close); window.addEventListener('resize', close);
    document.addEventListener('scroll', close, true);
  });
  buttons()[0]?.focus();
}
