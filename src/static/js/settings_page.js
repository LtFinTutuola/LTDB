/**
 * settings_page.js
 * Client-side logic for the Settings configuration and User Management (IAM) page in LTDB.
 */
import * as api from './api.js';

function showToast(message, type = 'success') {
    const toast = document.createElement('div');
    toast.style.position = 'fixed';
    toast.style.bottom = '24px';
    toast.style.right = '24px';
    toast.style.padding = '12px 20px';
    toast.style.borderRadius = 'var(--radius-sm)';
    toast.style.background = type === 'error' ? 'var(--error)' : 'var(--bg-elevated)';
    toast.style.color = '#fff';
    toast.style.border = type === 'error' ? '1px solid var(--error)' : '1px solid var(--border-color)';
    toast.style.boxShadow = '0 8px 16px rgba(0,0,0,0.3)';
    toast.style.zIndex = '9999';
    toast.style.fontSize = '13px';
    toast.style.display = 'flex';
    toast.style.alignItems = 'center';
    toast.style.gap = '8px';
    toast.style.transition = 'opacity 0.25s ease';

    const icon = document.createElement('span');
    icon.className = 'material-symbols-rounded';
    icon.style.fontSize = '18px';
    icon.textContent = type === 'error' ? 'error' : 'check_circle';
    toast.appendChild(icon);

    const text = document.createElement('span');
    text.textContent = message;
    toast.appendChild(text);

    document.body.appendChild(toast);

    setTimeout(() => {
        toast.style.opacity = '0';
        setTimeout(() => toast.remove(), 250);
    }, 3500);
}

// State
let initialSettings = null;
let usersList = [];

// DOM references
const dom = {
    // Tabs
    tabBtnConfig: document.getElementById('tab-btn-config'),
    tabBtnUsers: document.getElementById('tab-btn-users'),
    settingsForm: document.getElementById('settings-form'),
    usersSection: document.getElementById('users-section'),

    // Settings inputs
    inputDb: document.getElementById('setting-db'),
    inputExcelPath: document.getElementById('setting-excel-path'),
    inputExcelCol: document.getElementById('setting-excel-col'),
    inputGeminiToken: document.getElementById('setting-gemini-token'),
    btnToggleToken: document.getElementById('btn-toggle-token'),
    iconToggleToken: document.getElementById('icon-toggle-token'),
    inputGeminiModel: document.getElementById('setting-gemini-model'),
    btnSave: document.getElementById('btn-save-settings'),
    btnReset: document.getElementById('btn-reset-settings'),
    loadingState: document.getElementById('loading-state'),

    // Users IAM
    createUserForm: document.getElementById('create-user-form'),
    newUsername: document.getElementById('new-user-username'),
    newPassword: document.getElementById('new-user-password'),
    newRole: document.getElementById('new-user-role'),
    usersTbody: document.getElementById('users-tbody'),
    btnRevokeAll: document.getElementById('btn-revoke-all'),
};

// --- Tab Navigation ---
function switchTab(target) {
    if (target === 'config') {
        dom.tabBtnConfig.style.borderBottom = '2px solid var(--accent)';
        dom.tabBtnConfig.style.color = 'var(--accent)';
        dom.tabBtnConfig.style.fontWeight = '600';

        dom.tabBtnUsers.style.borderBottom = '2px solid transparent';
        dom.tabBtnUsers.style.color = 'var(--text-muted)';
        dom.tabBtnUsers.style.fontWeight = '500';

        dom.settingsForm.style.display = 'block';
        dom.usersSection.style.display = 'none';
    } else {
        dom.tabBtnUsers.style.borderBottom = '2px solid var(--accent)';
        dom.tabBtnUsers.style.color = 'var(--accent)';
        dom.tabBtnUsers.style.fontWeight = '600';

        dom.tabBtnConfig.style.borderBottom = '2px solid transparent';
        dom.tabBtnConfig.style.color = 'var(--text-muted)';
        dom.tabBtnConfig.style.fontWeight = '500';

        dom.settingsForm.style.display = 'none';
        dom.usersSection.style.display = 'block';
        loadUsers();
    }
}

// --- Settings Logic ---
async function loadSettings() {
    dom.loadingState.style.display = 'flex';
    dom.settingsForm.style.opacity = '0.5';
    dom.btnSave.disabled = true;

    try {
        const data = await api.getSettings();
        initialSettings = { ...data };
        populateForm(data);
    } catch (err) {
        showToast(`Errore durante il caricamento delle impostazioni: ${err.message}`, 'error');
    } finally {
        dom.loadingState.style.display = 'none';
        dom.settingsForm.style.opacity = '1';
        dom.btnSave.disabled = false;
    }
}

function populateForm(data) {
    dom.inputDb.value = data.db_connection_string || '';
    dom.inputExcelPath.value = data.excel_file_path || '';
    dom.inputExcelCol.value = data.excel_code_column || '';
    dom.inputGeminiToken.value = data.api_key || '';
    dom.inputGeminiModel.value = data.codes_deduction_agent_model || '';
}

function toggleTokenVisibility() {
    const isPassword = dom.inputGeminiToken.type === 'password';
    dom.inputGeminiToken.type = isPassword ? 'text' : 'password';
    dom.iconToggleToken.textContent = isPassword ? 'visibility_off' : 'visibility';
}

async function handleSaveSettings(e) {
    e.preventDefault();

    const payload = {
        db_connection_string: dom.inputDb.value.trim(),
        excel_file_path: dom.inputExcelPath.value.trim(),
        excel_code_column: dom.inputExcelCol.value.trim(),
        api_key: dom.inputGeminiToken.value.trim(),
        codes_deduction_agent_model: dom.inputGeminiModel.value.trim(),
    };

    if (!payload.db_connection_string) {
        showToast('Il campo Database non può essere vuoto.', 'error');
        dom.inputDb.focus();
        return;
    }
    if (!payload.excel_file_path) {
        showToast('Il campo Percorso file excel vendite non può essere vuoto.', 'error');
        dom.inputExcelPath.focus();
        return;
    }
    if (!payload.excel_code_column) {
        showToast('Il campo Colonna inserimento barcode non può essere vuoto.', 'error');
        dom.inputExcelCol.focus();
        return;
    }
    if (!payload.api_key) {
        showToast('Il campo Password Gemini non può essere vuoto.', 'error');
        dom.inputGeminiToken.focus();
        return;
    }
    if (!payload.codes_deduction_agent_model) {
        showToast('Il campo Versione di gemini non può essere vuoto.', 'error');
        dom.inputGeminiModel.focus();
        return;
    }

    dom.btnSave.disabled = true;
    const originalText = dom.btnSave.innerHTML;
    dom.btnSave.innerHTML = `<span class="material-symbols-rounded" style="animation: spin 0.8s linear infinite;">sync</span> Salvataggio...`;

    try {
        const updated = await api.updateSettings(payload);
        initialSettings = { ...updated };
        populateForm(updated);
        showToast('Impostazioni salvate con successo.');
    } catch (err) {
        showToast(`Errore durante il salvataggio: ${err.message}`, 'error');
    } finally {
        dom.btnSave.disabled = false;
        dom.btnSave.innerHTML = originalText;
    }
}

function handleResetSettings() {
    if (initialSettings) {
        populateForm(initialSettings);
        showToast('Valori ripristinati allo stato iniziale.');
    }
}

// --- Users Management (IAM) Logic ---
async function loadUsers() {
    try {
        const res = await fetch('/api/v1/users');
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || 'Impossibile caricare gli utenti');
        }
        usersList = await res.json();
        renderUsersTable();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

function renderUsersTable() {
    dom.usersTbody.innerHTML = '';
    if (!usersList.length) {
        dom.usersTbody.innerHTML = `<tr><td colspan="4" style="padding: 16px; text-align: center; color: var(--text-muted);">Nessun utente trovato.</td></tr>`;
        return;
    }

    usersList.forEach((u) => {
        const tr = document.createElement('tr');
        tr.style.borderBottom = '1px solid var(--border-color)';

        tr.innerHTML = `
            <td style="padding: 12px 8px; font-weight: 500;">
                ${escapeHtml(u.username)}
            </td>
            <td style="padding: 12px 8px;">
                <select class="form-control user-role-select" data-user-id="${u.id}" style="padding: 4px 8px; font-size: 12px; width: auto;">
                    <option value="base" ${u.role === 'base' ? 'selected' : ''}>Base</option>
                    <option value="admin" ${u.role === 'admin' ? 'selected' : ''}>Admin</option>
                </select>
            </td>
            <td style="padding: 12px 8px;">
                <label style="display: flex; align-items: center; gap: 6px; cursor: pointer;">
                    <input type="checkbox" class="user-active-toggle" data-user-id="${u.id}" ${u.is_active ? 'checked' : ''}>
                    <span style="color: ${u.is_active ? '#3fb950' : '#8b949e'}; font-size: 12px;">
                        ${u.is_active ? 'Attivo' : 'Disattivato'}
                    </span>
                </label>
            </td>
            <td style="padding: 12px 8px; text-align: right;">
                <button type="button" class="btn-secondary btn-reset-user-pw" data-user-id="${u.id}" data-username="${escapeHtml(u.username)}" style="padding: 4px 10px; font-size: 12px;">
                    <span class="material-symbols-rounded" style="font-size: 16px;">key</span>
                    Imposta Password
                </button>
            </td>
        `;

        dom.usersTbody.appendChild(tr);
    });

    // Attach event listeners for row actions
    document.querySelectorAll('.user-role-select').forEach((sel) => {
        sel.addEventListener('change', async (e) => {
            const userId = e.target.getAttribute('data-user-id');
            const newRole = e.target.value;
            await updateUserField(userId, { role: newRole });
        });
    });

    document.querySelectorAll('.user-active-toggle').forEach((chk) => {
        chk.addEventListener('change', async (e) => {
            const userId = e.target.getAttribute('data-user-id');
            const isActive = e.target.checked;
            await updateUserField(userId, { is_active: isActive });
        });
    });

    document.querySelectorAll('.btn-reset-user-pw').forEach((btn) => {
        btn.addEventListener('click', (e) => {
            const userId = btn.getAttribute('data-user-id');
            const username = btn.getAttribute('data-username');
            promptResetPassword(userId, username);
        });
    });
}

async function updateUserField(userId, payload) {
    try {
        const res = await fetch(`/api/v1/users/${userId}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });

        const data = await res.json();
        if (!res.ok) {
            throw new Error(data.detail || 'Operazione non riuscita');
        }
        showToast('Profilo utente aggiornato');
        loadUsers();
    } catch (err) {
        showToast(err.message, 'error');
        loadUsers(); // Revert UI
    }
}

function promptResetPassword(userId, username) {
    const newPass = prompt(`Inserisci la nuova password per l'utente "${username}" (minimo 4 caratteri):`);
    if (!newPass) return;
    if (newPass.length < 4) {
        alert('La password deve contenere almeno 4 caratteri.');
        return;
    }

    fetch(`/api/v1/users/${userId}/password`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ new_password: newPass }),
    })
        .then(async (res) => {
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || 'Errore nel reset');
            showToast(`Password impostata per ${username}`);
        })
        .catch((err) => showToast(err.message, 'error'));
}

async function handleCreateUser(e) {
    e.preventDefault();
    const username = dom.newUsername.value.trim().toLowerCase();
    const password = dom.newPassword.value;
    const role = dom.newRole.value;

    if (username.length < 4 || password.length < 4) {
        showToast('Nome utente e password devono contenere almeno 4 caratteri', 'error');
        return;
    }

    try {
        const res = await fetch('/api/v1/users', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password, role }),
        });

        const data = await res.json();
        if (!res.ok) {
            throw new Error(data.detail || 'Errore creazione utente');
        }

        showToast(`Utente "${username}" creato con successo`);
        dom.createUserForm.reset();
        loadUsers();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

async function handleRevokeAll() {
    if (!confirm('Sei sicuro di voler disconnettere TUTTE le sessioni del punto vendita? Tutti i dispositivi (incluso questo) dovranno effettuare nuovamente il login.')) {
        return;
    }

    try {
        const res = await fetch('/api/v1/users/revoke-all', {
            method: 'POST',
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Operazione fallita');
        alert('Tutte le sessioni sono state revocate. Verrai reindirizzato al login.');
        window.location.replace('/login');
    } catch (err) {
        showToast(err.message, 'error');
    }
}

function escapeHtml(str) {
    if (!str) return '';
    return str.replace(/[&<>'"]/g, (tag) => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        "'": '&#39;',
        '"': '&quot;',
    }[tag] || tag));
}

// --- Initialization ---
document.addEventListener('DOMContentLoaded', () => {
    loadSettings();

    // Tabs
    dom.tabBtnConfig.addEventListener('click', () => switchTab('config'));
    dom.tabBtnUsers.addEventListener('click', () => switchTab('users'));

    // Config form
    dom.btnToggleToken.addEventListener('click', toggleTokenVisibility);
    dom.settingsForm.addEventListener('submit', handleSaveSettings);
    dom.btnReset.addEventListener('click', handleResetSettings);

    // Users form
    dom.createUserForm.addEventListener('submit', handleCreateUser);
    dom.btnRevokeAll.addEventListener('click', handleRevokeAll);
});
