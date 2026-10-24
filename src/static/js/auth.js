/**
 * auth.js
 * Global authentication interceptor and session management for LTDB.
 * Intercepts 401 responses on fetch() and handles logout / password change.
 */

(function () {
  const originalFetch = window.fetch;

  window.fetch = async function (...args) {
    const res = await originalFetch.apply(this, args);
    const url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';

    // If unauthorized and not attempting to log in, redirect immediately to login
    if (res.status === 401 && !url.includes('/api/v1/auth/login')) {
      const currentPath = window.location.pathname;
      if (currentPath !== '/login') {
        window.location.replace('/login?next=' + encodeURIComponent(currentPath));
      }
    }
    return res;
  };
})();

export async function logoutUser(everywhere = false) {
  try {
    await fetch(`/api/v1/auth/logout?everywhere=${everywhere ? 'true' : 'false'}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    });
  } catch (err) {
    console.warn('Logout network error:', err);
  } finally {
    window.location.replace('/login');
  }
}

export function openChangePasswordModal() {
  let modal = document.getElementById('change-password-modal');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'change-password-modal';
    modal.className = 'auth-modal-backdrop';
    modal.innerHTML = `
      <div class="auth-modal-card">
        <div class="auth-modal-header">
          <h3>Modifica Password</h3>
          <button type="button" class="auth-modal-close" id="close-cp-modal">&times;</button>
        </div>
        <div id="cp-error" class="auth-modal-error" style="display:none;"></div>
        <div id="cp-success" class="auth-modal-success" style="display:none;"></div>
        <form id="cp-form" class="auth-modal-form">
          <div class="form-group">
            <label class="form-label" for="cp-current">Password attuale</label>
            <input type="password" id="cp-current" class="form-input" required autocomplete="current-password">
          </div>
          <div class="form-group">
            <label class="form-label" for="cp-new">Nuova password</label>
            <input type="password" id="cp-new" class="form-input" required minlength="6" autocomplete="new-password">
          </div>
          <div class="form-group">
            <label class="form-label" for="cp-confirm">Conferma nuova password</label>
            <input type="password" id="cp-confirm" class="form-input" required minlength="6" autocomplete="new-password">
          </div>
          <div class="auth-modal-actions">
            <button type="button" class="btn btn--secondary" id="cancel-cp-btn">Annulla</button>
            <button type="submit" class="btn btn--primary" id="save-cp-btn">Aggiorna Password</button>
          </div>
        </form>
      </div>
    `;
    document.body.appendChild(modal);

    const closeBtn = modal.querySelector('#close-cp-modal');
    const cancelBtn = modal.querySelector('#cancel-cp-btn');
    const form = modal.querySelector('#cp-form');
    const errBox = modal.querySelector('#cp-error');
    const succBox = modal.querySelector('#cp-success');

    const closeModal = () => {
      modal.style.display = 'none';
      form.reset();
      errBox.style.display = 'none';
      succBox.style.display = 'none';
    };

    closeBtn.addEventListener('click', closeModal);
    cancelBtn.addEventListener('click', closeModal);
    modal.addEventListener('click', (e) => {
      if (e.target === modal) closeModal();
    });

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      errBox.style.display = 'none';
      succBox.style.display = 'none';

      const curPass = document.getElementById('cp-current').value;
      const newPass = document.getElementById('cp-new').value;
      const confPass = document.getElementById('cp-confirm').value;

      if (newPass !== confPass) {
        errBox.textContent = 'Le nuove password non coincidono.';
        errBox.style.display = 'block';
        return;
      }

      try {
        const res = await fetch('/api/v1/auth/change-password', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ current_password: curPass, new_password: newPass }),
        });

        const data = await res.json();
        if (!res.ok) {
          errBox.textContent = data.detail || 'Impossibile modificare la password.';
          errBox.style.display = 'block';
          return;
        }

        succBox.textContent = 'Password aggiornata con successo!';
        succBox.style.display = 'block';
        setTimeout(() => {
          closeModal();
        }, 1500);
      } catch (err) {
        errBox.textContent = 'Errore di connessione col server.';
        errBox.style.display = 'block';
      }
    });
  }

  modal.style.display = 'flex';
}

// Attach logout and change password handlers when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
  const logoutBtn = document.getElementById('user-logout-btn');
  if (logoutBtn) {
    logoutBtn.addEventListener('click', (e) => {
      e.preventDefault();
      logoutUser();
    });
  }

  const cpBtn = document.getElementById('user-change-password-btn');
  if (cpBtn) {
    cpBtn.addEventListener('click', (e) => {
      e.preventDefault();
      openChangePasswordModal();
    });
  }
});
