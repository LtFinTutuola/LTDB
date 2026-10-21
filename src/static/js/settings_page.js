/**
 * settings_page.js
 * Client-side logic for the Settings configuration page in LTDB.
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

// DOM references
const dom = {
    form: document.getElementById('settings-form'),
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
};

async function loadSettings() {
    dom.loadingState.style.display = 'flex';
    dom.form.style.opacity = '0.5';
    dom.btnSave.disabled = true;

    try {
        const data = await api.getSettings();
        initialSettings = { ...data };
        populateForm(data);
    } catch (err) {
        showToast(`Errore durante il caricamento delle impostazioni: ${err.message}`, 'error');
    } finally {
        dom.loadingState.style.display = 'none';
        dom.form.style.opacity = '1';
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

document.addEventListener('DOMContentLoaded', () => {
    loadSettings();

    dom.btnToggleToken.addEventListener('click', toggleTokenVisibility);
    dom.form.addEventListener('submit', handleSaveSettings);
    dom.btnReset.addEventListener('click', handleResetSettings);
});
