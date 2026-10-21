import * as api from './api.js';
import * as staging from './staging.js';
import { addMessage, enterSplitScreen, exitSplitScreen, _esc } from './main.js';
import { setupBarcodeScanner } from './barcode_scanner.js';

let _activeJobId = null;
let _pollingInterval = null;

export function renderForms() {
    const ddtContainer = document.getElementById('ddt-form-container');
    const articleContainer = document.getElementById('article-form-container');
    if (!ddtContainer || !articleContainer) return;
    
    const brands = (window.__LTDB_BRANDS__ || [])
        .slice()
        .sort((a, b) => a.name.localeCompare(b.name, 'it', { sensitivity: 'base' }));
    const brandOptions = brands.map(b => `<option value="${_esc(b.id)}">${_esc(b.name)}</option>`).join('');

    
    ddtContainer.innerHTML = `
        <div class="import-form">
            <h2 style="margin-bottom:20px;">Importa DDT</h2>
            <div class="form-group">
                <label>Brand</label>
                <select id="ddt-brand" class="form-control">
                    <option value="">-- Seleziona brand --</option>
                    ${brandOptions}
                </select>
            </div>
            <div class="form-group">
                <label>Percorso file PDF (sul server)</label>
                <input id="ddt-filepath" class="form-control" type="text" placeholder="es. /data/ddt/documento.pdf">
            </div>
            <button id="btn-submit-ddt" class="btn-primary" style="width: 100%; margin-top: 20px;">Importa</button>
            <div id="ddt-status" style="margin-top: 15px; font-weight: 500;"></div>
        </div>
    `;
    
    articleContainer.innerHTML = `
        <div class="import-form">
            <h2 style="margin-bottom:20px;">Importa Articolo Singolo</h2>
            <div class="form-group">
                <label>Brand</label>
                <select id="single-brand" class="form-control">
                    <option value="">-- Seleziona brand --</option>
                    ${brandOptions}
                </select>
            </div>
            <div class="form-group">
                <label>Codice Fornitore *</label>
                <input id="single-vendor" class="form-control" type="text" placeholder="Inserisci il codice del fornitore">
            </div>
            <div class="form-group">
                <label>Nome Articolo</label>
                <input id="single-name" class="form-control" type="text" placeholder="Inserisci il nome dell'articolo">
            </div>
            <div class="form-group">
                <label>Barcode / EAN</label>
                <div class="barcode-input-wrapper">
                    <input id="single-barcode" class="form-control" type="text" placeholder="Inserisci il barcode o EAN">
                    <button type="button" id="btn-scan-single-barcode" class="btn-scan-field btn-scan-field--mobile-only" title="Scansiona barcode con la fotocamera">
                        <span class="material-symbols-rounded" style="font-size: 18px;">photo_camera</span>
                    </button>
                </div>
            </div>
            <div class="form-group">
                <label>Quantità</label>
                <input id="single-qty" class="form-control" type="number" value="1" min="1">
            </div>
            <div class="form-group">
                <label>Colore</label>
                <input id="single-colors" class="form-control" type="text" placeholder="Es: Rosso, Blu">
            </div>
            <button id="btn-submit-single" class="btn-primary" style="width: 100%; margin-top: 20px;">Importa</button>
            <div id="single-status" style="margin-top: 15px; font-weight: 500;"></div>
        </div>
    `;
    
    document.getElementById('btn-submit-ddt').addEventListener('click', submitDDTForm);
    document.getElementById('btn-submit-single').addEventListener('click', submitSingleItemForm);

    setupBarcodeScanner({
        cameraInputId: 'single-barcode-camera-input',
        triggerBtnIds: ['btn-scan-single-barcode'],
        onSuccess: (barcode) => {
            const barcodeInput = document.getElementById('single-barcode');
            if (barcodeInput) {
                barcodeInput.value = barcode;
                barcodeInput.dispatchEvent(new Event('input', { bubbles: true }));
            }
        },
        onError: () => {},
        showToastFn: (msg, type) => {
            addMessage(msg, type === 'error' ? 'error' : 'system');
        }
    });
}

function _val(id) { return document.getElementById(id)?.value?.trim() ?? ''; }

async function submitDDTForm() {
    const brandId = _val('ddt-brand');
    const filePath = _val('ddt-filepath');
    const statusEl = document.getElementById('ddt-status');
    const btn = document.getElementById('btn-submit-ddt');

    if (!brandId || !filePath) {
        addMessage('⚠️ Compila tutti i campi obbligatori.', 'error');
        return;
    }

    btn.disabled = true;
    statusEl.innerHTML = '<span class="spinner"></span> Avvio importazione...';
    try {
        const { job_id } = await api.extractDDT(filePath, brandId);
        startPolling(job_id, statusEl, btn);
    } catch (err) {
        statusEl.innerHTML = `<span style="color:var(--error);">❌ Errore: ${err.message}</span>`;
        addMessage(`Errore importazione DDT: ${err.message}`, 'error');
        btn.disabled = false;
    }
}

async function submitSingleItemForm() {
    const brandId = _val('single-brand');
    const vendorCode = _val('single-vendor');
    const statusEl = document.getElementById('single-status');
    const btn = document.getElementById('btn-submit-single');

    if (!brandId || !vendorCode) {
        addMessage('⚠️ Compila i campi obbligatori (Brand e Codice Fornitore).', 'error');
        return;
    }

    const payload = {
        brand_id: brandId,
        vendor_code: vendorCode,
        article_name: _val('single-name'),
        barcode: _val('single-barcode'),
        quantity: parseInt(_val('single-qty'), 10) || 1,
        colors: _val('single-colors') ? _val('single-colors').split(',').map(s=>s.trim()) : []
    };

    btn.disabled = true;
    statusEl.innerHTML = '<span class="spinner"></span> Avvio importazione...';
    try {
        const { job_id } = await api.ingestSingleItem(payload);
        startPolling(job_id, statusEl, btn);
    } catch (err) {
        statusEl.innerHTML = `<span style="color:var(--error);">❌ Errore: ${err.message}</span>`;
        addMessage(`Errore importazione: ${err.message}`, 'error');
        btn.disabled = false;
    }
}

function startPolling(jobId, statusEl, btn) {
    if (_pollingInterval) clearInterval(_pollingInterval);
    _activeJobId = jobId;
    
    _pollingInterval = setInterval(async () => {
        try {
            const statusData = await api.pollJobStatus(jobId);
            if (statusData.status === 'COMPLETED') {
                clearInterval(_pollingInterval);
                _pollingInterval = null;
                _activeJobId = null;
                
                statusEl.innerHTML = `<span style="color:var(--status-success);">Elaborazione completata.</span>`;
                btn.disabled = false;
                
                enterSplitScreen();
                staging.renderStagingPanel(statusData.data, jobId);
            } else if (statusData.status === 'ERROR') {
                clearInterval(_pollingInterval);
                _pollingInterval = null;
                _activeJobId = null;
                const errMsg = statusData.data?.error || 'Errore sconosciuto';
                statusEl.innerHTML = `<span style="color:var(--error);">❌ Fallito: ${errMsg}</span>`;
                btn.disabled = false;
            } else {
                statusEl.innerHTML = `<span class="spinner"></span> Elaborazione in corso...`;
            }
        } catch (err) {
            console.error('Polling error:', err);
        }
    }, 2000);
}

document.addEventListener('ingestionDone', () => {
    exitSplitScreen();

    // Reset DDT form
    if (document.getElementById('ddt-brand')) document.getElementById('ddt-brand').value = '';
    if (document.getElementById('ddt-filepath')) document.getElementById('ddt-filepath').value = '';
    if (document.getElementById('ddt-status')) document.getElementById('ddt-status').innerHTML = '';

    // Reset Single Article form
    if (document.getElementById('single-brand')) document.getElementById('single-brand').value = '';
    if (document.getElementById('single-vendor')) document.getElementById('single-vendor').value = '';
    if (document.getElementById('single-name')) document.getElementById('single-name').value = '';
    if (document.getElementById('single-barcode')) document.getElementById('single-barcode').value = '';
    if (document.getElementById('single-qty')) document.getElementById('single-qty').value = '1';
    if (document.getElementById('single-colors')) document.getElementById('single-colors').value = '';
    if (document.getElementById('single-status')) document.getElementById('single-status').innerHTML = '';
});
