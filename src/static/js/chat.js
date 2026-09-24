/**
 * chat.js
 * Handles the import flow: plus menu, form rendering, submission, and polling.
 */
import * as api from './api.js';
import {
    appState, Mode, setMode,
    addMessage, enterSplitScreen,
    showChatInput, clearFormContainer, getFormContainer,
    getDetailPanel, setChatInputLocked, _esc,
} from './main.js';
import * as staging from './staging.js';

let _plusMenuEl = null;
let _activeForm = null; // { type: 'ddt'|'single' }

// ---- Init ----
export function init() {
    // nothing async needed at init
}

// ---- Plus Menu ----
export function togglePlusMenu() {
    if (_plusMenuEl) { closePlusMenu(); return; }

    const inputArea = document.querySelector('.chat-input-area');
    _plusMenuEl = document.createElement('div');
    _plusMenuEl.className = 'plus-menu';
    _plusMenuEl.innerHTML = `
        <div class="plus-menu__item" data-action="ddt">📄 Importa DDT</div>
        <div class="plus-menu__item" data-action="single">📦 Importa Articolo</div>
    `;
    _plusMenuEl.addEventListener('click', (e) => {
        const action = e.target.closest('[data-action]')?.dataset.action;
        if (action === 'ddt') _showFormDDT();
        if (action === 'single') _showFormSingle();
        closePlusMenu();
    });
    inputArea.appendChild(_plusMenuEl);
}

export function closePlusMenu() {
    if (_plusMenuEl) { _plusMenuEl.remove(); _plusMenuEl = null; }
}

// ---- Forms ----
function _buildBrandOptions() {
    return appState.brands
        .map(b => `<option value="${_esc(b.id)}">${_esc(b.name)}</option>`)
        .join('');
}

function _showFormDDT() {
    showChatInput(false);
    _activeForm = { type: 'ddt' };
    setMode(Mode.FORM_DDT);

    const container = getFormContainer();
    container.innerHTML = `
        <div class="import-form" id="import-form">
            <div class="import-form__title">Importa DDT</div>
            <div class="form-group">
                <label>Brand</label>
                <select id="f-brand" class="form-control">
                    <option value="">-- Seleziona brand --</option>
                    ${_buildBrandOptions()}
                </select>
            </div>
            <div class="form-group">
                <label>Percorso file PDF (sul server)</label>
                <input id="f-filepath" class="form-control" type="text"
                       placeholder="es. /data/ddt/documento.pdf">
            </div>
        </div>
    `;
}

function _showFormSingle() {
    showChatInput(false);
    _activeForm = { type: 'single' };
    setMode(Mode.FORM_SINGLE);

    const container = getFormContainer();
    container.innerHTML = `
        <div class="import-form" id="import-form">
            <div class="import-form__title">Importa Articolo</div>
            <div class="form-group">
                <label>Brand</label>
                <select id="f-brand" class="form-control">
                    <option value="">-- Seleziona brand --</option>
                    ${_buildBrandOptions()}
                </select>
            </div>
            <div class="form-group">
                <label>Codice Fornitore *</label>
                <input id="f-vendor" class="form-control" type="text" placeholder="Inserisci il codice del fornitore, così come è riportato sul cartellino">
            </div>
            <div class="form-group">
                <label>Nome Articolo</label>
                <input id="f-name" class="form-control" type="text" placeholder="Inserisci il nome dell'articolo, così come è riportato sul cartellino">
            </div>
            <div class="form-group">
                <label>Barcode / EAN</label>
                <input id="f-barcode" class="form-control" type="text" placeholder="Inserisci il barcode o EAN, così come è riportato sul cartellino">
            </div>
            <div class="form-group">
                <label>Quantità</label>
                <input id="f-qty" class="form-control" type="number" value="1" min="1">
            </div>
            <div class="form-group">
                <label>Colore</label>
                <input id="f-colors" class="form-control" type="text" placeholder="Inserisci il colore così come è riportato sul cartellino (se presente). Se l'articolo ha più colori, inseriscili separati da una virgola.">
            </div>
        </div>
    `;
}

function _val(id) { return document.getElementById(id)?.value?.trim() ?? ''; }

// ---- Form submissions (called by main.js handleSend) ----
export async function submitDDTForm() {
    const brandId = _val('f-brand');
    const filePath = _val('f-filepath');

    if (!brandId || !filePath) {
        addMessage('⚠️ Compila tutti i campi obbligatori.', 'error');
        return;
    }

    _startPollingMode();
    try {
        const { job_id } = await api.extractDDT(filePath, brandId);
        appState.currentJobId = job_id;
        addMessage('⏳ DDT in elaborazione...', 'system');
        _startPolling(job_id);
    } catch (err) {
        addMessage(`❌ Errore: ${err.message}`, 'error');
        _resetToIdle();
    }
}

export async function submitSingleItemForm() {
    const brandId = _val('f-brand');
    const vendor = _val('f-vendor');
    const name = _val('f-name');
    const barcode = _val('f-barcode') || null;
    const qty = parseInt(_val('f-qty') || '1', 10);
    const colorsRaw = _val('f-colors');
    const colors = colorsRaw ? colorsRaw.split(',').map(s => s.trim().toLowerCase()).filter(Boolean) : [];

    if (!brandId || !vendor) {
        addMessage('⚠️ Brand e Codice Fornitore sono obbligatori.', 'error');
        return;
    }
    if (colors.length === 0) {
        addMessage('⚠️ Inserisci almeno un colore.', 'error');
        return;
    }

    _startPollingMode();
    const payload = {
        brand_id: brandId,
        vendor_code: vendor,
        article_name: name || null,
        barcode,
        quantity: qty,
        colors,
    };

    try {
        const { job_id } = await api.ingestSingleItem(payload);
        appState.currentJobId = job_id;
        addMessage('⏳ Articolo in elaborazione...', 'system');
        _startPolling(job_id);
    } catch (err) {
        addMessage(`❌ Errore: ${err.message}`, 'error');
        _resetToIdle();
    }
}

// ---- Polling ----
function _startPollingMode() {
    clearFormContainer();
    setMode(Mode.POLLING);
}

function _startPolling(jobId) {
    if (appState.pollingTimer) clearInterval(appState.pollingTimer);
    appState.pollingTimer = setInterval(() => _pollOnce(jobId), 2000);
}

async function _pollOnce(jobId) {
    try {
        const result = await api.pollJobStatus(jobId);
        if (result.status === 'COMPLETED') {
            clearInterval(appState.pollingTimer);
            appState.pollingTimer = null;
            addMessage('✅ Dati pronti per la revisione.', 'success');
            enterSplitScreen();
            staging.renderStagingPanel(result.data, jobId);
            setMode(Mode.STAGING);
        } else if (result.status === 'ERROR') {
            clearInterval(appState.pollingTimer);
            appState.pollingTimer = null;
            const errMsg = result.data?.error || 'errore sconosciuto';
            addMessage(`❌ Elaborazione fallita: ${errMsg}`, 'error');
            _resetToIdle();
        }
        // status === 'accepted' → keep polling silently
    } catch (err) {
        clearInterval(appState.pollingTimer);
        appState.pollingTimer = null;
        addMessage(`❌ Errore polling: ${err.message}`, 'error');
        _resetToIdle();
    }
}

function _resetToIdle() {
    showChatInput(true);
    setMode(Mode.IDLE);
}
