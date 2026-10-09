/* Login page behavior. Kept separate from static/auth.js (which runs on the
 * main app shell); this only loads on /login. */
(function () {
  const err = document.getElementById('login_err');
  const hint = document.getElementById('login_hint');
  const sub = document.getElementById('login_sub');
  const btn = document.getElementById('login_go');
  const user = document.getElementById('login_user');
  const pass = document.getElementById('login_pass');
  const totpBox = document.getElementById('login_totp_box');
  const totp = document.getElementById('login_totp');

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
        body: JSON.stringify({ username: user.value, password: pass.value,
                               totp: totp && totp.value ? totp.value.trim() : undefined }),
      });
      const d = await r.json();
      if (!r.ok) {
        // a module asked for a second factor: reveal the code field and resend with it
        if (d.second_factor && totpBox) {
          totpBox.classList.remove('hidden');
          err.textContent = r.status === 403 ? '' : (d.error || 'Login failed');
          hint.textContent = 'Enter the code from your authenticator app (or a backup code).';
          btn.disabled = false;
          totp.focus();
          return;
        }
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
  if (totp) totp.addEventListener('keydown', e => { if (e.key === 'Enter') login(); });
})();