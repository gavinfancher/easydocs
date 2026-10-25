// ---------- sign-in ----------
// Signing in (login.html) gets a session token from the API. It's kept in localStorage and sent
// as `Authorization: Bearer <token>` with every API call (api() in common.js). The pages hold no
// docs; the API checks the token on every call, and when it says no (never signed in, expired,
// signed out elsewhere) the page sends you to sign in.

const API_BASE = window.EASYDOCS_API || '';

const auth = (() => {
  const KEY = 'easydocs.session';
  // localStorage can be missing or throw (private windows, blocked storage); then a sign-in
  // still works, it just doesn't survive a reload.
  const token = () => { try { return localStorage.getItem(KEY); } catch { return null; } };
  const setToken = t => { try { t ? localStorage.setItem(KEY, t) : localStorage.removeItem(KEY); } catch { } };

  /** Headers for an API call, with the token if there is one. */
  const headers = (h = {}) => token() ? { ...h, Authorization: 'Bearer ' + token() } : h;

  function login() {
    setToken(null);
    location.assign('/admin/login?next=' + encodeURIComponent(location.pathname + location.search));
    return new Promise(() => { });  // the page is navigating away
  }

  /** Resolves to null when signed in, or the API's reason when not. */
  async function signIn(password) {
    try {
      const res = await fetch(API_BASE + '/api/login', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) return body.detail || `Sign-in failed (${res.status})`;
      setToken(body.token);
      return null;
    } catch { return 'Couldn’t reach easydocs. Try again in a moment.'; }
  }

  async function logout() {
    try { await fetch(API_BASE + '/api/logout', { method: 'POST', headers: headers() }); } catch { }
    setToken(null);
    location.assign('/admin/login');
  }

  document.addEventListener('DOMContentLoaded', () => {
    const btn = document.getElementById('sign-out');
    if (btn && token()) { btn.hidden = false; btn.onclick = logout; }
  });

  return { token, headers, login, signIn, logout };
})();
