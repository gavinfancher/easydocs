// ---------- sign-in ----------
// The server does the real work: every page and API needs a session cookie, which
// /admin/login sets. This only shows the Sign out button, and sends you back to the login page
// if your session ends while a page is open (see api() in common.js).

const auth = (() => {
  function login() {
    location.assign('/admin/login?next=' + encodeURIComponent(location.pathname + location.search));
    return new Promise(() => { });  // the page is navigating away
  }

  async function logout() {
    await fetch('/admin/logout', { method: 'POST' });
    location.assign('/admin/login');
  }

  document.addEventListener('DOMContentLoaded', async () => {
    const btn = document.getElementById('sign-out');
    if (!btn) return;
    try {
      const s = await (await fetch('/api/session')).json();
      if (s.login_required) { btn.hidden = false; btn.onclick = logout; }
    } catch { }
  });

  return { login, logout };
})();
