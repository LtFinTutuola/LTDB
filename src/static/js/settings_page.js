/**
 * settings_page.js
 * Client-side logic for the Settings page in LTDB:
 * - Tab 1 (Admin only): System (Database) & Google Gemini AI configuration.
 * - Tab 2 (Admin & Cassa): Cash Register Excel file path, column mapping (article code,
 *   phone number, customer info, discount OTP), live Edge Forwarder diagnostics, and Force Sync.
 * - Tab 3 (Admin only): User Management (IAM) & session revocation.
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

const currentUser = window.__LTDB_USER__ || { role: 'admin' };
const isAdmin = currentUser.role === 'admin';

// State
let initialSystemSettings = null;
let initialExcelSettings = null;
let usersList = [];
let statusPollTimer = null;

// DOM references
const dom = {
    // Tabs & Sections
    tabBtnConfig: document.getElementById('tab-btn-config'),
    tabBtnExcel: document.getElementById('tab-btn-excel'),
    tabBtnUsers: document.getElementById('tab-btn-users'),
    settingsForm: document.getElementById('settings-form'),
    excelSection: document.getElementById('excel-settings-section'),
    usersSection: document.getElementById('users-section'),
    loadingState: document.getElementById('loading-state'),

    // System & Gemini inputs (Admin only)
    inputDb: document.getElementById('setting-db'),
    inputGeminiToken: document.getElementById('setting-gemini-token'),
    btnToggleToken: document.getElementById('btn-toggle-token'),
    iconToggleToken: document.getElementById('icon-toggle-token'),
    inputGeminiModel: document.getElementById('setting-gemini-model'),
    btnSave: document.getElementById('btn-save-settings'),
    btnReset: document.getElementById('btn-reset-settings'),

    // Excel inputs (Admin & Cassa)
    excelForm: document.getElementById('excel-settings-form'),
    inputExcelPath: document.getElementById('setting-excel-path'),
    inputExcelCol: document.getElementById('setting-excel-col'),
    inputExcelPhoneCol: document.getElementById('setting-excel-phone-col'),
    inputExcelStatusCol: document.getElementById('setting-excel-status-col'),
    inputExcelOtpCol: document.getElementById('setting-excel-otp-col'),
    btnSaveExcel: document.getElementById('btn-save-excel-settings'),
    btnResetExcel: document.getElementById('btn-reset-excel-settings'),
    btnForceSync: document.getElementById('btn-force-sync'),
    btnRefreshAgentStatus: document.getElementById('btn-refresh-agent-status'),
    statusReadable: document.getElementById('agent-status-readable'),
    statusMonitored: document.getElementById('agent-status-monitored'),
    statusReported: document.getElementById('agent-status-reported'),
    statusPending: document.getElementById('agent-status-pending'),

    // Users IAM (Admin only)
    createUserForm: document.getElementById('create-user-form'),
    newUsername: document.getElementById('new-user-username'),
    newPassword: document.getElementById('new-user-password'),
    newRole: document.getElementById('new-user-role'),
    usersTbody: document.getElementById('users-tbody'),
    btnRevokeAll: document.getElementById('btn-revoke-all'),
};

function setTabButtonStyle(btn, active) {
    if (!btn) return;
    btn.style.borderBottom = active ? '2px solid var(--accent)' : '2px solid transparent';
    btn.style.color = active ? 'var(--accent)' : 'var(--text-muted)';
    btn.style.fontWeight = active ? '600' : '500';
}

function stopStatusPolling() {
    if (statusPollTimer) {
        clearInterval(statusPollTimer);
        statusPollTimer = null;
    }
}

function startStatusPolling() {
    stopStatusPolling();
    loadAgentStatus();
    statusPollTimer = setInterval(loadAgentStatus, 5000);
}

// --- Tab Navigation ---
function switchTab(target) {
    if (!isAdmin && target !== 'excel') {
        target = 'excel';
    }

    setTabButtonStyle(dom.tabBtnConfig, target === 'config');
    setTabButtonStyle(dom.tabBtnExcel, target === 'excel');
    setTabButtonStyle(dom.tabBtnUsers, target === 'users');

    if (dom.settingsForm) dom.settingsForm.style.display = target === 'config' ? 'block' : 'none';
    if (dom.excelSection) dom.excelSection.style.display = target === 'excel' ? 'block' : 'none';
    if (dom.usersSection) dom.usersSection.style.display = target === 'users' ? 'block' : 'none';

    if (target === 'excel') {
        startStatusPolling();
    } else {
        stopStatusPolling();
    }

    if (target === 'users' && isAdmin) {
        loadUsers();
    }
}

// --- Admin System & Gemini Settings Logic ---
async function loadSystemSettings() {
    if (!isAdmin || !dom.settingsForm) return;
    dom.loadingState.style.display = 'flex';
    dom.settingsForm.style.opacity = '0.5';
    if (dom.btnSave) dom.btnSave.disabled = true;

    try {
        const data = await api.getSettings();
        initialSystemSettings = { ...data };
        populateSystemForm(data);
    } catch (err) {
        showToast(`Errore durante il caricamento delle impostazioni: ${err.message}`, 'error');
    } finally {
        dom.loadingState.style.display = 'none';
        dom.settingsForm.style.opacity = '1';
        if (dom.btnSave) dom.btnSave.disabled = false;
    }
}

function populateSystemForm(data) {
    if (!dom.inputDb) return;
    dom.inputDb.value = data.db_connection_string || '';
    dom.inputGeminiToken.value = data.api_key || '';
    dom.inputGeminiModel.value = data.codes_deduction_agent_model || '';
}

function toggleTokenVisibility() {
    if (!dom.inputGeminiToken) return;
    const isPassword = dom.inputGeminiToken.type === 'password';
    dom.inputGeminiToken.type = isPassword ? 'text' : 'password';
    dom.iconToggleToken.textContent = isPassword ? 'visibility_off' : 'visibility';
}

async function handleSaveSystemSettings(e) {
    e.preventDefault();
    if (!isAdmin) return;

    const payload = {
        db_connection_string: dom.inputDb.value.trim(),
        api_key: dom.inputGeminiToken.value.trim(),
        codes_deduction_agent_model: dom.inputGeminiModel.value.trim(),
    };

    if (!payload.db_connection_string) {
        showToast('Il campo Database non può essere vuoto.', 'error');
        dom.inputDb.focus();
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
        initialSystemSettings = { ...updated };
        populateSystemForm(updated);
        showToast('Configurazioni di sistema salvate con successo.');
    } catch (err) {
        showToast(`Errore durante il salvataggio: ${err.message}`, 'error');
    } finally {
        dom.btnSave.disabled = false;
        dom.btnSave.innerHTML = originalText;
    }
}

function handleResetSystemSettings() {
    if (initialSystemSettings) {
        populateSystemForm(initialSystemSettings);
        showToast('Valori di sistema ripristinati.');
    }
}

// --- Excel Settings & Force Sync Logic (Admin & Cassa) ---
async function loadExcelSettings() {
    if (!dom.excelForm) return;
    dom.loadingState.style.display = 'flex';
    dom.excelForm.style.opacity = '0.5';
    if (dom.btnSaveExcel) dom.btnSaveExcel.disabled = true;

    try {
        const data = await api.getExcelSettings();
        initialExcelSettings = { ...data };
        populateExcelForm(data);
    } catch (err) {
        showToast(`Errore caricamento impostazioni Excel: ${err.message}`, 'error');
    } finally {
        dom.loadingState.style.display = 'none';
        dom.excelForm.style.opacity = '1';
        if (dom.btnSaveExcel) dom.btnSaveExcel.disabled = false;
    }
}

function populateExcelForm(data) {
    if (!dom.inputExcelPath) return;
    dom.inputExcelPath.value = data.excel_file_path || '';
    dom.inputExcelCol.value = (data.excel_code_column || 'N').toUpperCase();
    dom.inputExcelPhoneCol.value = (data.excel_phone_column || 'O').toUpperCase();
    dom.inputExcelStatusCol.value = (data.excel_status_column || 'P').toUpperCase();
    dom.inputExcelOtpCol.value = (data.excel_otp_column || 'Q').toUpperCase();
}

function isValidExcelCol(val) {
    return /^[A-Z]{1,2}$/.test(val);
}

async function handleSaveExcelSettings(e) {
    e.preventDefault();

    const payload = {
        excel_file_path: dom.inputExcelPath.value.trim(),
        excel_code_column: dom.inputExcelCol.value.trim().toUpperCase(),
        excel_phone_column: dom.inputExcelPhoneCol.value.trim().toUpperCase(),
        excel_status_column: dom.inputExcelStatusCol.value.trim().toUpperCase(),
        excel_otp_column: dom.inputExcelOtpCol.value.trim().toUpperCase(),
    };

    if (!payload.excel_file_path) {
        showToast('Il campo Percorso file Excel vendite non può essere vuoto.', 'error');
        dom.inputExcelPath.focus();
        return;
    }

    const colChecks = [
        [payload.excel_code_column, 'Colonna codice articolo', dom.inputExcelCol],
        [payload.excel_phone_column, 'Colonna numero di telefono', dom.inputExcelPhoneCol],
        [payload.excel_status_column, 'Colonna info cliente', dom.inputExcelStatusCol],
        [payload.excel_otp_column, 'Colonna codici sconto', dom.inputExcelOtpCol],
    ];
    for (const [val, label, el] of colChecks) {
        if (!isValidExcelCol(val)) {
            showToast(`${label}: inserisci una lettera di colonna valida (es. N, O, P, Q).`, 'error');
            el.focus();
            return;
        }
    }

    const uniqueCols = new Set(colChecks.map(([v]) => v));
    if (uniqueCols.size !== colChecks.length) {
        showToast('Le 4 colonne configurate devono essere tutte distinte tra loro.', 'error');
        return;
    }

    dom.btnSaveExcel.disabled = true;
    const originalText = dom.btnSaveExcel.innerHTML;
    dom.btnSaveExcel.innerHTML = `<span class="material-symbols-rounded" style="animation: spin 0.8s linear infinite;">sync</span> Salvataggio...`;

    try {
        const updated = await api.updateExcelSettings(payload);
        initialExcelSettings = { ...updated };
        populateExcelForm(updated);
        showToast('Impostazioni Excel salvate e inviate al forwarder.');
        loadAgentStatus();
    } catch (err) {
        showToast(`Errore durante il salvataggio: ${err.message}`, 'error');
    } finally {
        dom.btnSaveExcel.disabled = false;
        dom.btnSaveExcel.innerHTML = originalText;
    }
}

function handleResetExcelSettings() {
    if (initialExcelSettings) {
        populateExcelForm(initialExcelSettings);
        showToast('Impostazioni Excel ripristinate.');
    }
}

async function loadAgentStatus() {
    if (!dom.statusReadable) return;
    try {
        const res = await api.getExcelSyncStatus();
        const edge = (res && res.data && res.data.edge_agent) || {};

        if (!edge.reported_at) {
            dom.statusReadable.textContent = 'Forwarder non connesso';
            dom.statusReadable.style.color = 'var(--text-muted)';
        } else if (edge.file_readable) {
            dom.statusReadable.textContent = 'Attivo e leggibile';
            dom.statusReadable.style.color = '#3fb950';
        } else {
            dom.statusReadable.textContent = 'File non trovato / non di oggi';
            dom.statusReadable.style.color = '#d29922';
        }

        dom.statusMonitored.textContent = edge.monitored_file || edge.configured_file || '—';
        dom.statusReported.textContent = edge.reported_at
            ? new Date(edge.reported_at).toLocaleTimeString('it-IT')
            : '—';
        dom.statusPending.textContent = String(edge.pending_commands_count ?? 0);
    } catch (err) {
        // Silent background refresh failure
    }
}

async function handleForceSync() {
    if (!dom.btnForceSync) return;
    dom.btnForceSync.disabled = true;
    const originalHtml = dom.btnForceSync.innerHTML;
    dom.btnForceSync.innerHTML = `<span class="material-symbols-rounded" style="animation: spin 0.8s linear infinite;">sync</span> Invio comando...`;

    try {
        await api.forceExcelSync();
        showToast('Comando di sincronizzazione forzata accodato per il forwarder.');
        await loadAgentStatus();
    } catch (err) {
        showToast(`Errore durante l'invio del comando: ${err.message}`, 'error');
    } finally {
        dom.btnForceSync.disabled = false;
        dom.btnForceSync.innerHTML = originalHtml;
    }
}

// --- Users Management (IAM) Logic (Admin only) ---
async function loadUsers() {
    if (!isAdmin || !dom.usersTbody) return;
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
    if (!dom.usersTbody) return;
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
                    <option value="cassa" ${u.role === 'cassa' ? 'selected' : ''}>Cassa</option>
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
        btn.addEventListener('click', () => {
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
        loadUsers();
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
    loadExcelSettings();
    if (isAdmin) {
        loadSystemSettings();
    }

    // Tabs
    if (dom.tabBtnConfig) dom.tabBtnConfig.addEventListener('click', () => switchTab('config'));
    if (dom.tabBtnExcel) dom.tabBtnExcel.addEventListener('click', () => switchTab('excel'));
    if (dom.tabBtnUsers) dom.tabBtnUsers.addEventListener('click', () => switchTab('users'));

    // Admin System & Gemini form
    if (dom.btnToggleToken) dom.btnToggleToken.addEventListener('click', toggleTokenVisibility);
    if (dom.settingsForm) dom.settingsForm.addEventListener('submit', handleSaveSystemSettings);
    if (dom.btnReset) dom.btnReset.addEventListener('click', handleResetSystemSettings);

    // Excel Settings & Force Sync
    if (dom.excelForm) dom.excelForm.addEventListener('submit', handleSaveExcelSettings);
    if (dom.btnResetExcel) dom.btnResetExcel.addEventListener('click', handleResetExcelSettings);
    if (dom.btnForceSync) dom.btnForceSync.addEventListener('click', handleForceSync);
    if (dom.btnRefreshAgentStatus) dom.btnRefreshAgentStatus.addEventListener('click', loadAgentStatus);

    // Admin Users form
    if (dom.createUserForm) dom.createUserForm.addEventListener('submit', handleCreateUser);
    if (dom.btnRevokeAll) dom.btnRevokeAll.addEventListener('click', handleRevokeAll);

    // Initial active tab
    if (!isAdmin || window.location.hash === '#excel') {
        switchTab('excel');
    } else {
        switchTab('config');
    }
});
