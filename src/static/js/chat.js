/**
 * chat.js
 * Handles the import flow: plus menu, form rendering, submission, and polling.
 * Generates Artifact Boxes for background jobs.
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

// Track job data for artifact boxes
const _jobDataMap = new Map();

// ---- Init ----
export function init() {
    // Listen for panel closed event to make artifact box clickable
    document.addEventListener('detail:closed', () => {
        document.querySelectorAll('.artifact-box').forEach(box => {
            const jid = box.dataset.jobId;
            if (_jobDataMap.has(jid)) {
                box.classList.add('artifact-box--clickable');
            }
        });
    });

    // Quando l'importazione viene confermata definitivamente
    document.addEventListener('ingestionDone', async (e) => {
        const jobId = e.detail?.jobId;
        if (!jobId) return;

        // Async polling: 3 attempts, 1 sec interval
        for (let i = 0; i < 3; i++) {
            try {
                await new Promise(r => setTimeout(r, 1000));
                await api.pollJobStatus(jobId);
                // If it succeeds (returns status), the job is still there. Wait for the next loop.
            } catch (err) {
                // If it fails (e.g. 404), the job was deleted!
                const box = document.querySelector(`.artifact-box[data-job-id="${jobId}"]`);
                if (box) {
                    box.classList.remove('artifact-box--clickable');
                    const icon = box.querySelector('.artifact-box__open-icon');
                    if (icon) icon.remove();
                    updateArtifactBoxStatus(jobId, '✅ Completato');
                }
                _jobDataMap.delete(jobId);
                return;
            }
        }
        console.warn(`Job ${jobId} not removed from staging after 3 attempts. Box remains active.`);
    });
}

// ---- Plus Menu ----
export function togglePlusMenu() {
    if (_plusMenuEl) { closePlusMenu(); return; }

    const inputArea = document.querySelector('.chat-input-area');
    _plusMenuEl = document.createElement('div');
    _plusMenuEl.className = 'plus-menu';
    _plusMenuEl.innerHTML = `
        <div class="plus-menu__item" data-action="ddt">
            <span class="material-symbols-rounded">description</span> Importa DDT
        </div>
        <div class="plus-menu__item" data-action="single">
            <span class="material-symbols-rounded">inventory_2</span> Importa Articolo
        </div>
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

// ---- Artifact Box ----
function emitArtifactBox(title, jobId, type) {
    const box = document.createElement('div');
    box.className = 'artifact-box';
    box.dataset.jobId = jobId;

    const icon = type === 'ddt' ? 'description' : 'inventory_2';

    box.innerHTML = `
        <span class="material-symbols-rounded artifact-box__icon">${icon}</span>
        <div class="artifact-box__body">
            <span class="artifact-box__title">${title}</span>
            <span class="artifact-box__status"><span class="spinner"></span>In elaborazione...</span>
        </div>
        <span class="material-symbols-rounded artifact-box__open-icon">open_in_full</span>
    `;

    box.addEventListener('click', () => {
        if (box.classList.contains('artifact-box--clickable') && _jobDataMap.has(jobId)) {
            enterSplitScreen();
            staging.renderStagingPanel(_jobDataMap.get(jobId), jobId);
            setMode(Mode.STAGING);
            box.classList.remove('artifact-box--clickable');
        }
    });

    addMessage(box, 'system');
}

function updateArtifactBoxStatus(jobId, status, isError = false) {
    const box = document.querySelector(`.artifact-box[data-job-id="${jobId}"]`);
    if (!box) return;
    const statusEl = box.querySelector('.artifact-box__status');
    if (statusEl) {
        statusEl.innerHTML = status;
        if (isError) statusEl.style.color = 'var(--error)';
        else statusEl.style.color = 'var(--success)';
    }
}


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
        emitArtifactBox('Importazione DDT', job_id, 'ddt');
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
        emitArtifactBox('Importazione Articolo', job_id, 'single');
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
            updateArtifactBoxStatus(jobId, '✅ Da confermare');
            _jobDataMap.set(jobId, result.data);
            enterSplitScreen();
            staging.renderStagingPanel(result.data, jobId);
            setMode(Mode.STAGING);
        } else if (result.status === 'ERROR') {
            clearInterval(appState.pollingTimer);
            appState.pollingTimer = null;
            const errMsg = result.data?.error || 'errore sconosciuto';
            updateArtifactBoxStatus(jobId, '❌ Elaborazione fallita', true);
            addMessage(`❌ Elaborazione fallita: ${errMsg}`, 'error');
            _resetToIdle();
        }
        // status === 'accepted' → keep polling silently
    } catch (err) {
        clearInterval(appState.pollingTimer);
        appState.pollingTimer = null;
        updateArtifactBoxStatus(jobId, '❌ Errore', true);
        addMessage(`❌ Errore polling: ${err.message}`, 'error');
        _resetToIdle();
    }
}

function _resetToIdle() {
    showChatInput(true);
    setMode(Mode.IDLE);
}
