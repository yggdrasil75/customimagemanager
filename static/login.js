/* Login page behavior. Kept separate from static/auth.js (which runs on the
 * main app shell); this only loads on /login. */
(function () {
  const err = document.getElementById('login_err');
  const hint = document.getElementById('login_hint');
  const sub = document.getElementById('login_sub');
  const btn = document.getElementById('login_go');
  const user = document.getElementById('login_user');
  const pass = document.getElementById('login_pass');

  fetch('/api/auth/config')
    .then(r => r.json())
    .then(c => {
      if (c.needs_bootstrap) {
        sub.textContent = 'First-run setup';
        hint.textContent = 'No accounts exist yet. The username and password ' +
          'you enter now will create the initial administrator account.';
      } else {
        hint.textContent = 'Authentication mode: ' + c.mode;
      }
    })
    .catch(() => {});

  async function login() {
    err.textContent = '';
    btn.disabled = true;
    try {
      const r = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: user.value, password: pass.value }),
      });
      const d = await r.json();
      if (!r.ok) {
        err.textContent = d.error || 'Login failed';
        btn.disabled = false;
        return;
      }
      location.href = '/';
    } catch (e) {
      err.textContent = 'Network error';
      btn.disabled = false;
    }
  }

  btn.onclick = login;
  pass.addEventListener('keydown', e => { if (e.key === 'Enter') login(); });
})();