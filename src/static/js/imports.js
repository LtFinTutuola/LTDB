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
    const rawVendor = _val('single-vendor');
    const vendorCode = rawVendor.trim().replace(/\s+/g, ' ').toUpperCase();
    const rawBarcode = _val('single-barcode');
    const barcode = rawBarcode.replace(/\s+/g, '');
    const rawName = _val('single-name');
    const articleName = rawName.trim().replace(/\s+/g, ' ');
    const rawColors = _val('single-colors');
    const colors = rawColors ? rawColors.split(',').map(s => s.trim().replace(/\s+/g, '')).filter(Boolean) : [];
    const statusEl = document.getElementById('single-status');
    const btn = document.getElementById('btn-submit-single');

    // Update input display with cleaned values
    const vendorInput = document.getElementById('single-vendor');
    if (vendorInput) vendorInput.value = vendorCode;
    const barcodeInput = document.getElementById('single-barcode');
    if (barcodeInput) barcodeInput.value = barcode;

    if (!brandId || !vendorCode) {
        addMessage('⚠️ Compila i campi obbligatori (Brand e Codice Fornitore).', 'error');
        return;
    }

    const payload = {
        brand_id: brandId,
        vendor_code: vendorCode,
        article_name: articleName,
        barcode: barcode,
        quantity: parseInt(_val('single-qty'), 10) || 1,
        colors: colors
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

function showTypoWarningModal(d, onEdit, onConfirm) {
    const modal = document.getElementById('modal-typo-warning');
    if (!modal) {
        const proceed = window.confirm(
            `⚠️ Possibile refuso nel codice fornitore:\n` +
            `Codice inserito: ${d.suspect_code || ''}\n` +
            `Modello simile esistente: ${d.suggested_match || ''} (${d.blueprint_name || ''})\n` +
            `${d.reason || ''}\n\n` +
            `Clicca 'Annulla' per correggere il codice, o 'OK' per procedere comunque.`
        );
        if (proceed) onConfirm();
        else onEdit();
        return;
    }

    const suspectEl = document.getElementById('typo-modal-suspect-code');
    const suggestedEl = document.getElementById('typo-modal-suggested-code');
    const bpNameEl = document.getElementById('typo-modal-blueprint-name');
    const reasonEl = document.getElementById('typo-modal-reason');

    if (suspectEl) suspectEl.textContent = d.suspect_code || '-';
    if (suggestedEl) suggestedEl.textContent = d.suggested_match || '-';
    if (bpNameEl) bpNameEl.textContent = d.blueprint_name ? `(${d.blueprint_name})` : '';
    if (reasonEl) reasonEl.textContent = d.reason || 'Distanza minima da un codice esistente';

    const btnEdit = document.getElementById('btn-typo-modal-edit');
    const btnConfirm = document.getElementById('btn-typo-modal-confirm');
    const btnClose = document.getElementById('btn-close-typo-warning');

    const cleanup = () => {
        modal.classList.remove('active');
        btnEdit?.removeEventListener('click', handleEdit);
        btnConfirm?.removeEventListener('click', handleConfirm);
        btnClose?.removeEventListener('click', handleEdit);
        modal.removeEventListener('click', handleBackdrop);
        document.removeEventListener('keydown', handleKey);
    };

    const handleEdit = () => {
        cleanup();
        onEdit();
    };

    const handleConfirm = () => {
        cleanup();
        onConfirm();
    };

    const handleBackdrop = (e) => {
        if (e.target === modal) {
            handleEdit();
        }
    };

    const handleKey = (e) => {
        if (e.key === 'Escape') {
            handleEdit();
        }
    };

    btnEdit?.addEventListener('click', handleEdit);
    btnConfirm?.addEventListener('click', handleConfirm);
    btnClose?.addEventListener('click', handleEdit);
    modal.addEventListener('click', handleBackdrop);
    document.addEventListener('keydown', handleKey);

    modal.classList.add('active');
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
            } else if (statusData.status === 'TYPO_WARNING') {
                clearInterval(_pollingInterval);
                _pollingInterval = null;
                _activeJobId = null;
                btn.disabled = false;
                const d = statusData.data || {};

                showTypoWarningModal(
                    d,
                    () => {
                        const vendorInput = document.getElementById('single-vendor');
                        if (vendorInput) {
                            vendorInput.focus();
                            vendorInput.select();
                        }
                        statusEl.innerHTML = `<span style="color:var(--text-muted);">Verifica il codice fornitore e premi di nuovo Importa.</span>`;
                    },
                    async () => {
                        statusEl.innerHTML = `<span class="spinner"></span> Invio con conferma in corso...`;
                        btn.disabled = true;
                        try {
                            const payload = {
                                brand_id: _val('single-brand'),
                                vendor_code: _val('single-vendor'),
                                article_name: _val('single-name') || null,
                                barcode: _val('single-barcode') || null,
                                quantity: parseInt(_val('single-qty') || '1', 10),
                                colors: _val('single-colors') ? _val('single-colors').split(',').map(s => s.trim()).filter(Boolean) : [],
                                typo_warning_confirmed: true
                            };
                            const { job_id } = await api.ingestSingleItem(payload);
                            startPolling(job_id, statusEl, btn);
                        } catch (e) {
                            statusEl.innerHTML = `<span style="color:var(--error);">❌ Errore: ${e.message}</span>`;
                            btn.disabled = false;
                        }
                    }
                );
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

    // Reset Single Article form (preservando brand e colore fissi per importazioni multiple)
    if (document.getElementById('single-vendor')) document.getElementById('single-vendor').value = '';
    if (document.getElementById('single-name')) document.getElementById('single-name').value = '';
    if (document.getElementById('single-barcode')) document.getElementById('single-barcode').value = '';
    if (document.getElementById('single-qty')) document.getElementById('single-qty').value = '1';
    if (document.getElementById('single-status')) document.getElementById('single-status').innerHTML = '';
});
