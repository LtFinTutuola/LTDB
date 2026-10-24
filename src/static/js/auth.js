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
          <button type="button" class="auth-modal-close" id="close-cp-modal" aria-label="Chiudi">&times;</button>
        </div>
        <div id="cp-error" class="auth-modal-error" style="display:none;"></div>
        <div id="cp-success" class="auth-modal-success" style="display:none;"></div>
        <form id="cp-form" class="auth-modal-form">
          <div class="form-group">
            <label class="form-label" for="cp-current">Password attuale</label>
            <input type="password" id="cp-current" class="form-input" required autocomplete="current-password" placeholder="Inserisci password attuale">
          </div>
          <div class="form-group">
            <label class="form-label" for="cp-new">Nuova password</label>
            <input type="password" id="cp-new" class="form-input" required minlength="4" autocomplete="new-password" placeholder="Minimo 4 caratteri">
          </div>
          <div class="form-group">
            <label class="form-label" for="cp-confirm">Conferma nuova password</label>
            <input type="password" id="cp-confirm" class="form-input" required minlength="4" autocomplete="new-password" placeholder="Ripeti nuova password">
          </div>
          <div class="auth-modal-actions">
            <button type="button" class="auth-btn-cancel" id="cancel-cp-btn">Annulla</button>
            <button type="submit" class="auth-btn-submit" id="save-cp-btn">Aggiorna Password</button>
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

      if (newPass.length < 4) {
        errBox.textContent = 'La nuova password deve contenere almeno 4 caratteri.';
        errBox.style.display = 'block';
        return;
      }

      if (newPass !== confPass) {
        errBox.textContent = 'Le nuove password non coincidono.';
        errBox.style.display = 'block';
        return;
      }

      const submitBtn = modal.querySelector('#save-cp-btn');
      submitBtn.disabled = true;
      const originalText = submitBtn.textContent;
      submitBtn.textContent = 'Salvataggio...';

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
        }, 1200);
      } catch (err) {
        errBox.textContent = 'Errore di connessione col server.';
        errBox.style.display = 'block';
      } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = originalText;
      }
    });
  }

  modal.style.display = 'flex';
  const curInput = modal.querySelector('#cp-current');
  if (curInput) setTimeout(() => curInput.focus(), 60);
}

export function openMobileUserSheet() {
  const sheet = document.getElementById('mobile-user-sheet');
  if (sheet) {
    sheet.style.display = 'flex';
    const mobileUserBtn = document.getElementById('mobile-user-btn');
    if (mobileUserBtn) mobileUserBtn.classList.add('mobile-nav-item--active');
  }
}

export function closeMobileUserSheet() {
  const sheet = document.getElementById('mobile-user-sheet');
  if (sheet) {
    sheet.style.display = 'none';
    const mobileUserBtn = document.getElementById('mobile-user-btn');
    if (mobileUserBtn) mobileUserBtn.classList.remove('mobile-nav-item--active');
  }
}

// Global Delegated Click Handler for bulletproof responsiveness across desktop & mobile
document.addEventListener('click', (e) => {
  // 1. Password change trigger (desktop sidebar, mobile sheet, etc.)
  const cpBtn = e.target.closest('#user-change-password-btn, #mobile-btn-change-password, [data-action="change-password"]');
  if (cpBtn) {
    e.preventDefault();
    closeMobileUserSheet();
    openChangePasswordModal();
    return;
  }

  // 2. Logout trigger (desktop sidebar, mobile sheet, etc.)
  const logoutBtn = e.target.closest('#user-logout-btn, #mobile-btn-logout, [data-action="logout"]');
  if (logoutBtn) {
    e.preventDefault();
    closeMobileUserSheet();
    logoutUser();
    return;
  }

  // 3. Mobile user navigation trigger
  const mobileUserBtn = e.target.closest('#mobile-user-btn');
  if (mobileUserBtn) {
    e.preventDefault();
    openMobileUserSheet();
    return;
  }

  // 4. Mobile user sheet close button or backdrop
  const sheetCloseBtn = e.target.closest('#mobile-user-sheet-close');
  if (sheetCloseBtn) {
    e.preventDefault();
    closeMobileUserSheet();
    return;
  }
  const sheetBackdrop = document.getElementById('mobile-user-sheet');
  if (sheetBackdrop && e.target === sheetBackdrop) {
    closeMobileUserSheet();
    return;
  }
});

// Expose utilities on window
window.openChangePasswordModal = openChangePasswordModal;
window.openMobileUserSheet = openMobileUserSheet;
window.closeMobileUserSheet = closeMobileUserSheet;
window.logoutUser = logoutUser;
