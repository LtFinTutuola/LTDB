/**
 * blueprint_revision.js
 * ---------------------
 * Desktop-only Visual Blueprint Separation, Natural-Language Rule Review/Correction Loop,
 * Single-Blueprint "Prima e Dopo" Staging Area, and Manual Warehouse Anomalies Queue.
 *
 * Strictly hides all regular expression syntax from the operator.
 */

import * as api from './api.js?v=20261026_4';
import {
    enterSplitScreen,
    exitSplitScreen,
    getDetailPanel,
    addMessage,
    _esc,
} from './main.js?v=20261026_4';
import { requestPhotoUpload } from './photo_upload_helper.js?v=20261026_2';

let _currentContext = null;
let _buckets = []; // [{ group_id, label, is_original, user_hint, groupKeys: Set }]
let _groupByKey = {}; // key -> BlueprintArticleGroupSchema
let _currentJobId = null;
let _currentRuleResponse = null;
let _currentStagingJob = null;
let _onCompleteCallback = null;
let _activeAfterPhotoIndices = {};
let _skipRuleDeduction = false;
let _relatedBlueprintIds = [];

function _openImageModal(url) {
    if (!url) return;
    let overlay = document.getElementById('image-zoom-modal');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'image-zoom-modal';
        overlay.className = 'image-modal-overlay';
        overlay.innerHTML = `
            <div class="image-modal-content" style="position:fixed;top:0;left:0;width:100vw;height:100vh;background:rgba(0,0,0,0.85);z-index:99999;display:flex;align-items:center;justify-content:center;cursor:pointer;">
                <img id="image-zoom-img" src="" style="max-width:90vw;max-height:90vh;border-radius:8px;object-fit:contain;">
            </div>
        `;
        document.body.appendChild(overlay);
        overlay.onclick = () => {
            overlay.style.display = 'none';
        };
    }
    const img = overlay.querySelector('#image-zoom-img');
    if (img) img.src = url;
    overlay.style.display = 'block';
}
window._openImageModal = _openImageModal;

function _groupKey(g) {
    return g.group_key || `${g.supplier_code}__${(g.colors || []).join('|')}__${g.ean || ''}`;
}

/**
 * Refresh the "Anomalie Magazzino" header button badge count.
 */
export async function refreshWarehouseAnomaliesBadge() {
    const btn = document.getElementById('btn-warehouse-anomalies');
    const badge = document.getElementById('warehouse-anomalies-badge');
    if (!btn || !badge) return null;

    try {
        const res = await api.getWarehouseAnomalies();
        const count = res?.total_count ?? (res?.anomalies || []).length;
        badge.textContent = String(count);
        if (count > 0) {
            btn.style.display = 'inline-flex';
        } else {
            btn.style.display = 'none';
        }
        return res;
    } catch (err) {
        console.warn('Impossibile aggiornare il contatore anomalie magazzino:', err);
        return null;
    }
}

/**
 * Opens the modal listing all anomalous blueprints detected in the warehouse
 * so the operator can manually inspect and split/correct them one by one.
 */
export async function openWarehouseAnomaliesModal(onComplete = null) {
    if (window.innerWidth <= 1024) {
        addMessage('La gestione delle anomalie dei Blueprint è disponibile solo su desktop.', 'error');
        return;
    }

    _ensureAnomaliesModalContainer();
    const modal = document.getElementById('modal-warehouse-anomalies');
    const body = document.getElementById('warehouse-anomalies-modal-body');
    if (!modal || !body) return;

    modal.classList.add('active', 'open');
    body.innerHTML = `
        <div style="padding: 42px; text-align: center; color: var(--text-secondary);">
            <div class="spinner" style="margin: 0 auto 14px auto;"></div>
            <div>Verifica delle schede articolo rispetto alle regole attive dei brand...</div>
        </div>
    `;

    try {
        const res = await refreshWarehouseAnomaliesBadge();
        const items = res?.anomalies || [];

        if (items.length === 0) {
            body.innerHTML = `
                <div style="padding: 40px 24px; text-align: center; color: var(--text-secondary);">
                    <span class="material-symbols-rounded" style="font-size: 40px; color: #81c995; margin-bottom: 10px;">verified</span>
                    <h4 style="margin: 0 0 6px 0; color: var(--text-primary);">Nessuna anomalia rilevata in magazzino</h4>
                    <div style="font-size: 13px; color: var(--text-muted);">
                        Tutte le schede articolo sono allineate con le regole di classificazione dei rispettivi brand.
                    </div>
                </div>
            `;
            return;
        }

        body.innerHTML = `
            <div class="bp-rev-instructions" style="margin-bottom: 14px;">
                <span class="material-symbols-rounded" style="color: #f6ad55; font-size: 20px; flex-shrink: 0;">rule</span>
                <div>
                    Sono state rilevate <strong>${items.length}</strong> schede articolo in magazzino che presentano anomalie rispetto alla regola attiva del brand.
                    Seleziona <strong>Correggi Manualmente</strong> per aprire la schermata di selezione degli articoli e ricreare le schede corrette una per una (senza dover ricalcolare la regola del brand).
                </div>
            </div>
            <div style="display: flex; flex-direction: column; gap: 10px;">
                ${items
                    .map((item, idx) => {
                        const isSplit = item.anomaly_type === 'SPLIT_REQUIRED';
                        const badgeStyle = isSplit
                            ? 'background:rgba(246,173,85,0.16);color:#f6ad55;border:1px solid rgba(246,173,85,0.4);'
                            : 'background:rgba(129,201,149,0.16);color:#81c995;border:1px solid rgba(129,201,149,0.4);';
                        const badgeLabel = isSplit ? 'Articoli da Separare' : 'Possibile Duplicato';
                        const thumbHtml = item.photo_url
                            ? `<img src="${_esc(item.photo_url)}" alt="foto" style="width:48px;height:48px;border-radius:6px;object-fit:cover;flex-shrink:0;">`
                            : `<div style="width:48px;height:48px;border-radius:6px;background:var(--bg-elevated);display:flex;align-items:center;justify-content:center;color:var(--text-muted);flex-shrink:0;">
                                   <span class="material-symbols-rounded" style="font-size:22px;">image</span>
                               </div>`;
                        const codesPreview = (item.supplier_codes || []).join(', ');
                        return `
                            <div class="warehouse-anomaly-card" data-anomaly-idx="${idx}">
                                <div style="display:flex;align-items:center;gap:12px;min-width:0;flex:1;">
                                    ${thumbHtml}
                                    <div style="min-width:0;flex:1;">
                                        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:4px;">
                                            <span style="font-size:10.5px;font-weight:700;text-transform:uppercase;padding:2px 8px;border-radius:999px;${badgeStyle}">
                                                ${_esc(badgeLabel)}
                                            </span>
                                            <span style="font-size:12px;font-weight:600;color:var(--accent);">${_esc(item.brand_name)}</span>
                                            <span style="font-size:13.5px;font-weight:600;color:var(--text-primary);">${_esc(item.article_name)}</span>
                                        </div>
                                        <div style="font-size:12px;color:var(--text-secondary);margin-bottom:3px;">
                                            ${_esc(item.reason)}
                                        </div>
                                        <div style="font-size:11.5px;color:var(--text-muted);font-family:monospace;">
                                            Codici fornitore: ${_esc(codesPreview)}
                                        </div>
                                    </div>
                                </div>
                                <div style="display:flex;align-items:center;gap:8px;flex-shrink:0;">
                                    <button type="button" class="btn-secondary btn-close-warehouse-anomaly" data-anomaly-idx="${idx}" style="white-space:nowrap;padding:7px 12px;font-size:12px;display:inline-flex;align-items:center;gap:5px;">
                                        <span class="material-symbols-rounded" style="font-size:16px;color:#81c995;">check_circle</span>
                                        Chiudi Anomalia
                                    </button>
                                    <button type="button" class="btn-primary btn-fix-warehouse-anomaly" data-anomaly-idx="${idx}" style="white-space:nowrap;padding:7px 14px;font-size:12.5px;display:inline-flex;align-items:center;gap:6px;">
                                        <span class="material-symbols-rounded" style="font-size:16px;">call_split</span>
                                        Correggi Manualmente
                                    </button>
                                </div>
                            </div>
                        `;
                    })
                    .join('')}
            </div>
        `;

        body.querySelectorAll('.btn-close-warehouse-anomaly').forEach((btn) => {
            btn.addEventListener('click', async (e) => {
                const idx = parseInt(e.currentTarget.dataset.anomalyIdx, 10);
                const item = items[idx];
                if (!item) return;

                const originalHtml = btn.innerHTML;
                btn.disabled = true;
                btn.innerHTML = '<span class="spinner" style="width:14px;height:14px;"></span> Verifica...';

                try {
                    const res = await api.closeWarehouseAnomaly(item.blueprint_id);
                    addMessage(`✅ ${res.message}`, 'system');
                    await showWarehouseAnomaliesModal(onComplete);
                    await refreshWarehouseAnomaliesBadge();
                    if (typeof onComplete === 'function') onComplete();
                } catch (err) {
                    btn.disabled = false;
                    btn.innerHTML = originalHtml;
                    addMessage(`❌ ${err.message}`, 'error');
                    alert(`Impossibile chiudere l'anomalia:\n\n${err.message}`);
                }
            });
        });

        body.querySelectorAll('.btn-fix-warehouse-anomaly').forEach((btn) => {
            btn.addEventListener('click', (e) => {
                const idx = parseInt(e.currentTarget.dataset.anomalyIdx, 10);
                const item = items[idx];
                if (!item) return;
                modal.classList.remove('active', 'open');
                openBlueprintRevision(item.blueprint_id, onComplete, {
                    skipRuleDeduction: true,
                    relatedBlueprintIds: item.related_blueprint_ids || [],
                });
            });
        });
    } catch (err) {
        body.innerHTML = `
            <div style="padding: 32px; text-align: center; color: var(--error);">
                Errore nel caricamento delle anomalie: ${_esc(err.message)}
            </div>
        `;
    }
}

function _ensureAnomaliesModalContainer() {
    if (document.getElementById('modal-warehouse-anomalies')) return;

    const modal = document.createElement('div');
    modal.id = 'modal-warehouse-anomalies';
    modal.className = 'modal-backdrop bp-revision-desktop-only';
    modal.innerHTML = `
        <div class="modal-card bp-rev-modal-card" style="max-width: 860px !important;">
            <div class="modal-header bp-rev-modal-header">
                <div style="display: flex; align-items: center; gap: 10px;">
                    <span class="material-symbols-rounded" style="color: #f6ad55; font-size: 22px;">rule</span>
                    <div>
                        <h3 class="modal-title" style="margin: 0;">Anomalie Magazzino Rilevate</h3>
                        <div style="font-size: 12px; color: var(--text-muted); margin-top: 2px;">
                            Correggi manualmente o valida e chiudi le schede articolo anomale
                        </div>
                    </div>
                </div>
                <button id="btn-close-warehouse-anomalies-modal" class="modal-close" type="button" title="Chiudi">
                    <span class="material-symbols-rounded">close</span>
                </button>
            </div>
            <div id="warehouse-anomalies-modal-body" class="modal-body bp-rev-modal-body"></div>
            <div class="modal-footer bp-rev-modal-footer" style="justify-content: flex-end;">
                <button type="button" id="btn-dismiss-warehouse-anomalies-modal" class="btn-secondary">Chiudi</button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);

    const closeModal = () => modal.classList.remove('active', 'open');
    document.getElementById('btn-close-warehouse-anomalies-modal')?.addEventListener('click', closeModal);
    document.getElementById('btn-dismiss-warehouse-anomalies-modal')?.addEventListener('click', closeModal);
}

/**
 * Entry point triggered from the desktop Warehouse Catalog row button or Anomalies modal.
 * Opens the visual separation & rule review modal/workspace for a Blueprint.
 *
 * @param {string} blueprintId
 * @param {Function|null} onComplete
 * @param {Object} options - { skipRuleDeduction?: boolean, relatedBlueprintIds?: string[] }
 */
export async function openBlueprintRevision(blueprintId, onComplete = null, options = {}) {
    if (window.innerWidth <= 1024) {
        addMessage('La revisione e separazione dei Blueprint è disponibile solo su desktop.', 'error');
        return;
    }

    _skipRuleDeduction = Boolean(options.skipRuleDeduction);
    if (!_skipRuleDeduction) {
        try {
            const anomRes = await api.getWarehouseAnomalies();
            if (anomRes && anomRes.total_count > 0) {
                const msg = `Sono presenti ${anomRes.total_count} anomalie a magazzino non risolte. Risolvi o chiudi le anomalie aperte prima di dividere altri blueprint.`;
                addMessage(`⚠️ ${msg}`, 'error');
                alert(`Impossibile avviare la divisione del blueprint:\n\n${msg}`);
                return;
            }
        } catch (_) {}
    }

    _onCompleteCallback = onComplete;
    _currentJobId = null;
    _currentRuleResponse = null;
    _currentStagingJob = null;
    _relatedBlueprintIds = Array.isArray(options.relatedBlueprintIds) ? options.relatedBlueprintIds : [];


    _ensureModalContainer();
    const modal = document.getElementById('modal-blueprint-revision');
    const titleEl = document.getElementById('bp-rev-modal-title');
    const body = document.getElementById('bp-rev-modal-body');

    if (titleEl) {
        titleEl.textContent = _skipRuleDeduction
            ? 'Correzione Manuale Scheda Articolo (Senza Ricalcolo Regola)'
            : 'Separazione Articoli e Revisione Regola Brand';
    }

    if (modal && body) {
        modal.classList.add('active', 'open');
        body.innerHTML = `
            <div style="padding: 48px; text-align: center; color: var(--text-secondary);">
                <div class="spinner" style="margin: 0 auto 14px auto;"></div>
                <div>Caricamento articoli fisici associati al Blueprint...</div>
            </div>
        `;
    }

    try {
        const ctx = await api.getBlueprintRevisionContext(blueprintId, _relatedBlueprintIds);
        _currentContext = ctx;
        _initBucketsFromContext(ctx);
        _renderStep1BucketSelector();
    } catch (err) {
        if (modal) modal.classList.remove('active', 'open');
        addMessage(`Errore nel caricamento del Blueprint: ${err.message}`, 'error');
    }
}

function _initBucketsFromContext(ctx) {
    _groupByKey = {};
    const allKeys = [];
    (ctx.article_groups || []).forEach((g) => {
        const k = _groupKey(g);
        _groupByKey[k] = g;
        allKeys.push(k);
    });

    _buckets = [
        {
            group_id: 'group_original',
            label: ctx.article_name || 'Nuovo Modello #1',
            is_original: true,
            user_hint: '',
            groupKeys: new Set(allKeys),
        },
        {
            group_id: 'group_new_1',
            label: `${ctx.article_name || 'Modello'} (Nuovo Modello #2)`,
            is_original: false,
            user_hint: '',
            groupKeys: new Set(),
        },
    ];
}

function _ensureModalContainer() {
    if (document.getElementById('modal-blueprint-revision')) return;

    const modal = document.createElement('div');
    modal.id = 'modal-blueprint-revision';
    modal.className = 'modal-backdrop bp-revision-desktop-only';
    modal.innerHTML = `
        <div class="modal-card bp-rev-modal-card">
            <div class="modal-header bp-rev-modal-header">
                <div style="display: flex; align-items: center; gap: 10px;">
                    <span class="material-symbols-rounded" style="color: var(--accent); font-size: 22px;">call_split</span>
                    <div>
                        <h3 id="bp-rev-modal-title" class="modal-title" style="margin: 0;">Separazione Articoli e Revisione Regola Brand</h3>
                        <div id="bp-rev-modal-subtitle" style="font-size: 12px; color: var(--text-muted); margin-top: 2px;"></div>
                    </div>
                </div>
                <div id="bp-rev-step-indicator" class="bp-rev-steps"></div>
                <button id="btn-close-bp-rev-modal" class="modal-close" type="button" title="Chiudi">
                    <span class="material-symbols-rounded">close</span>
                </button>
            </div>
            <div id="bp-rev-modal-body" class="modal-body bp-rev-modal-body"></div>
            <div id="bp-rev-modal-footer" class="modal-footer bp-rev-modal-footer"></div>
        </div>
    `;
    document.body.appendChild(modal);

    document.getElementById('btn-close-bp-rev-modal').addEventListener('click', () => {
        modal.classList.remove('active', 'open');
    });
}

function _renderStepIndicator(activeStep) {
    const el = document.getElementById('bp-rev-step-indicator');
    if (!el) return;
    const steps = _skipRuleDeduction
        ? [
              { num: 1, label: '1. Separa Articoli' },
              { num: 3, label: '2. Prima e Dopo (Staging)' },
          ]
        : [
              { num: 1, label: '1. Separa Articoli' },
              { num: 2, label: '2. Verifica Regola AI' },
              { num: 3, label: '3. Prima e Dopo (Staging)' },
          ];
    el.innerHTML = steps
        .map(
            (s) => `
            <span class="bp-rev-step-pill ${s.num === activeStep ? 'bp-rev-step-pill--active' : s.num < activeStep ? 'bp-rev-step-pill--done' : ''}">
                ${_esc(s.label)}
            </span>
        `
        )
        .join('<span class="material-symbols-rounded" style="font-size:15px;color:var(--text-muted);">chevron_right</span>');
}

// ===========================================================================
// STEP 1: Visual Bucket Selector (Zero Regex Exposure)
// ===========================================================================

function _renderStep1BucketSelector() {
    _renderStepIndicator(1);

    const subtitle = document.getElementById('bp-rev-modal-subtitle');
    const totalPhysicalUnits = (_currentContext?.article_groups || []).reduce((acc, g) => acc + (g.quantity || 0), 0);
    if (subtitle && _currentContext) {
        subtitle.textContent = `Brand: ${_currentContext.brand_name} • Articolo: ${_currentContext.article_name} (${totalPhysicalUnits} unità fisiche)`;
    }

    const body = document.getElementById('bp-rev-modal-body');
    const footer = document.getElementById('bp-rev-modal-footer');
    if (!body || !footer || !_currentContext) return;

    const origBucket = _buckets[0];
    const newBuckets = _buckets.slice(1);

    const instructionsHtml = _skipRuleDeduction
        ? `
            <strong>Correzione Manuale (Regola Brand Già Attiva):</strong> Distribuisci gli articoli fisici nei modelli corretti.
            Tutte le nuove schede articolo verranno <strong>ricreate da zero tramite l'AI</strong> mantenendo le foto già associate agli articoli.
            ${
                _relatedBlueprintIds.length > 0
                    ? `<br><span style="color:#81c995;">Nota: Questa vista include gli articoli provenienti dalle schede duplicate rilevate. Puoi lasciarli tutti nella colonna di sinistra per unificarli in un'unica nuova scheda, oppure separarli.</span>`
                    : ''
            }
        `
        : `
            <strong>Come funziona:</strong> Qui sotto trovi tutti gli articoli fisici attualmente raggruppati in questa scheda.
            Sposta nella colonna di destra gli articoli che appartengono a un <strong>modello diverso</strong> (es. versione grande vs piccola, o linea differente).
            L'AI dedurrà la nuova regola per il brand <strong>${_esc(_currentContext.brand_name)}</strong> e ricreerà da zero le nuove schede articolo mantenendo le foto.
        `;

    body.innerHTML = `
        <div class="bp-rev-instructions">
            <span class="material-symbols-rounded" style="color: var(--accent); font-size: 20px; flex-shrink: 0;">info</span>
            <div>${instructionsHtml}</div>
        </div>

        <div class="bp-rev-columns">
            <!-- Left Column: First New Blueprint Bucket -->
            <div class="bp-rev-column bp-rev-column--source">
                <div class="bp-rev-column__header">
                    <div>
                        <span class="bp-rev-badge bp-rev-badge--orig">Nuovo Modello #1 (Da Ricreare)</span>
                        <h4 style="margin: 6px 0 2px 0; font-size: 15px; color: var(--text-primary);">${_esc(origBucket.label)}</h4>
                        <span style="font-size: 12px; color: var(--text-muted);">${origBucket.groupKeys.size} gruppi di articoli assegnati a questo modello</span>
                    </div>
                    <div style="margin-top: 8px;">
                        <input
                            type="text"
                            class="form-control bp-rev-bucket-hint-input"
                            data-bucket-idx="0"
                            value="${_esc(origBucket.user_hint)}"
                            placeholder="Indicazione facoltativa per l'AI (es. Modello Grande / Shopping)..."
                            style="font-size: 12.5px; padding: 6px 10px;"
                        >
                    </div>
                </div>
                <div class="bp-rev-cards-list">
                    ${_renderBucketCardsHtml(origBucket, 0)}
                </div>
            </div>

            <!-- Right Column: Target New Blueprint Buckets -->
            <div class="bp-rev-column bp-rev-column--targets">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
                    <span style="font-size: 13px; font-weight: 600; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.04em;">
                        Altri Modelli da Separare (${newBuckets.length})
                    </span>
                    <button type="button" id="btn-bp-rev-add-bucket" class="btn-secondary" style="padding: 5px 12px; font-size: 12px; display: inline-flex; align-items: center; gap: 4px;">
                        <span class="material-symbols-rounded" style="font-size: 16px;">add</span>
                        Aggiungi ulteriore modello
                    </button>
                </div>
                <div class="bp-rev-target-buckets-wrap">
                    ${newBuckets.map((b, idx) => _renderTargetBucketHtml(b, idx + 1)).join('')}
                </div>
            </div>
        </div>
    `;

    const primaryActionHtml = _skipRuleDeduction
        ? `
            <button type="button" id="btn-bp-rev-manual-stage" class="btn-primary" style="display: inline-flex; align-items: center; gap: 6px;">
                <span class="material-symbols-rounded" style="font-size: 18px;">compare_arrows</span>
                Genera Nuove Schede e Vai a Staging
            </button>
        `
        : `
            <button type="button" id="btn-bp-rev-deduce" class="btn-primary" style="display: inline-flex; align-items: center; gap: 6px;">
                <span class="material-symbols-rounded" style="font-size: 18px;">auto_awesome</span>
                Elabora Regola con l'AI
            </button>
        `;

    footer.innerHTML = `
        <button type="button" id="btn-bp-rev-cancel" class="btn-secondary">Annulla</button>
        ${primaryActionHtml}
    `;

    // Bind events
    document.getElementById('btn-bp-rev-cancel')?.addEventListener('click', () => {
        document.getElementById('modal-blueprint-revision')?.classList.remove('active', 'open');
    });

    document.getElementById('btn-bp-rev-add-bucket')?.addEventListener('click', () => {
        const nextNum = _buckets.length + 1;
        _buckets.push({
            group_id: `group_new_${_buckets.length}`,
            label: `${_currentContext.article_name} (Nuovo Modello #${nextNum})`,
            is_original: false,
            user_hint: '',
            groupKeys: new Set(),
        });
        _renderStep1BucketSelector();
    });

    // Bind bucket label / hint inputs
    body.querySelectorAll('.bp-rev-bucket-hint-input').forEach((input) => {
        input.addEventListener('input', (e) => {
            const bIdx = parseInt(e.target.dataset.bucketIdx, 10);
            if (_buckets[bIdx]) {
                _buckets[bIdx].user_hint = e.target.value;
                if (e.target.value.trim()) {
                    _buckets[bIdx].label = e.target.value.trim();
                }
            }
        });
    });

    // Bind remove bucket buttons
    body.querySelectorAll('.btn-bp-rev-remove-bucket').forEach((btn) => {
        btn.addEventListener('click', (e) => {
            const bIdx = parseInt(e.currentTarget.dataset.bucketIdx, 10);
            if (bIdx > 0 && _buckets.length > 2) {
                const removed = _buckets.splice(bIdx, 1)[0];
                removed.groupKeys.forEach((k) => _buckets[0].groupKeys.add(k));
                _renderStep1BucketSelector();
            }
        });
    });

    // Bind card move buttons
    body.querySelectorAll('.btn-bp-rev-move-card').forEach((btn) => {
        btn.addEventListener('click', (e) => {
            const key = e.currentTarget.dataset.groupKey;
            const fromIdx = parseInt(e.currentTarget.dataset.fromIdx, 10);
            const toIdx = parseInt(e.currentTarget.dataset.toIdx, 10);
            if (_buckets[fromIdx] && _buckets[toIdx] && key) {
                _buckets[fromIdx].groupKeys.delete(key);
                _buckets[toIdx].groupKeys.add(key);
                _renderStep1BucketSelector();
            }
        });
    });

    // Bind primary buttons
    document.getElementById('btn-bp-rev-deduce')?.addEventListener('click', async () => {
        await _handleDeduceRule(null);
    });

    document.getElementById('btn-bp-rev-manual-stage')?.addEventListener('click', async () => {
        await _handlePrepareManualSplit();
    });
}

function _renderTargetBucketHtml(bucket, bucketIdx) {
    const canRemove = _buckets.length > 2;
    const modelNumber = bucketIdx + 1;
    return `
        <div class="bp-rev-target-bucket">
            <div class="bp-rev-target-bucket__header">
                <div style="display: flex; align-items: center; justify-content: space-between; gap: 8px;">
                    <span class="bp-rev-badge bp-rev-badge--new">Nuovo Modello #${modelNumber} (Da Ricreare)</span>
                    ${
                        canRemove
                            ? `<button type="button" class="btn-bp-rev-remove-bucket" data-bucket-idx="${bucketIdx}" title="Rimuovi questo gruppo" style="background:transparent;border:none;color:var(--text-muted);cursor:pointer;">
                                <span class="material-symbols-rounded" style="font-size:18px;">close</span>
                               </button>`
                            : ''
                    }
                </div>
                <div style="margin-top: 8px;">
                    <input
                        type="text"
                        class="form-control bp-rev-bucket-hint-input"
                        data-bucket-idx="${bucketIdx}"
                        value="${_esc(bucket.user_hint)}"
                        placeholder="Indicazione facoltativa per l'AI (es. Modello Piccolo / Pochette)..."
                        style="font-size: 13px; padding: 7px 10px;"
                    >
                </div>
            </div>
            <div class="bp-rev-cards-list">
                ${_renderBucketCardsHtml(bucket, bucketIdx)}
            </div>
        </div>
    `;
}

function _renderBucketCardsHtml(bucket, bucketIdx) {
    const keys = Array.from(bucket.groupKeys);
    if (keys.length === 0) {
        return `
            <div class="bp-rev-empty-bucket">
                ${
                    bucket.is_original
                        ? 'Tutti gli articoli sono stati spostati. Lascia almeno un articolo nel Modello #1.'
                        : 'Clicca "Sposta qui" sugli articoli a sinistra per separarli in questo nuovo modello.'
                }
            </div>
        `;
    }

    return keys
        .map((key) => {
            const g = _groupByKey[key];
            if (!g) return '';
            const photoUrl = g.photo_id ? api.getPhotoUrl(g.photo_id) : '';
            const photoHtml = photoUrl
                ? `<img src="${_esc(photoUrl)}" alt="${_esc(g.supplier_code)}" style="width:100%;height:100%;object-fit:cover;">`
                : `<span class="material-symbols-rounded" style="color:var(--text-muted);font-size:20px;">image</span>`;
            const colorLabel = (g.colors || []).join(', ') || 'N/D';

            let moveBtnsHtml = '';
            if (bucket.is_original) {
                moveBtnsHtml = _buckets
                    .slice(1)
                    .map(
                        (targetB, idx) => `
                        <button type="button" class="btn-bp-rev-move-card bp-rev-move-btn bp-rev-move-btn--forward"
                            data-group-key="${_esc(key)}" data-from-idx="0" data-to-idx="${idx + 1}"
                            title="Sposta nel Nuovo Modello #${idx + 2}">
                            <span>Sposta in #${idx + 2}</span>
                            <span class="material-symbols-rounded" style="font-size:16px;">arrow_forward</span>
                        </button>
                    `
                    )
                    .join('');
            } else {
                moveBtnsHtml = `
                    <button type="button" class="btn-bp-rev-move-card bp-rev-move-btn bp-rev-move-btn--back"
                        data-group-key="${_esc(key)}" data-from-idx="${bucketIdx}" data-to-idx="0"
                        title="Riporta nel Modello #1">
                        <span class="material-symbols-rounded" style="font-size:16px;">arrow_back</span>
                        <span>Riporta in #1</span>
                    </button>
                `;
            }

            return `
                <div class="bp-rev-article-card">
                    <div class="bp-rev-article-card__photo">
                        ${photoHtml}
                    </div>
                    <div class="bp-rev-article-card__info">
                        <div class="bp-rev-article-card__code">${_esc(g.supplier_code)}</div>
                        <div class="bp-rev-article-card__meta">
                            <span>Colore: <strong>${_esc(colorLabel)}</strong></span>
                            ${g.ean ? `<span>• EAN: ${_esc(g.ean)}</span>` : ''}
                            <span>• Qtà: <strong>${g.quantity}</strong></span>
                        </div>
                    </div>
                    <div class="bp-rev-article-card__actions">
                        ${moveBtnsHtml}
                    </div>
                </div>
            `;
        })
        .join('');
}

function _collectActivePartitions() {
    const activePartitions = [];
    for (const b of _buckets) {
        if (b.groupKeys.size === 0) continue;
        const articleIds = [];
        const supplierCodes = [];
        b.groupKeys.forEach((k) => {
            const g = _groupByKey[k];
            if (g) {
                articleIds.push(...(g.article_ids || []));
                if (g.supplier_code && !supplierCodes.includes(g.supplier_code)) {
                    supplierCodes.push(g.supplier_code);
                }
            }
        });
        activePartitions.push({
            group_id: b.group_id,
            label: b.user_hint?.trim() || b.label,
            is_original: b.is_original,
            article_ids: articleIds,
            supplier_codes: supplierCodes,
            user_hint: b.user_hint?.trim() || null,
        });
    }
    return activePartitions;
}

async function _handlePrepareManualSplit() {
    const activePartitions = _collectActivePartitions();
    const minRequired = _relatedBlueprintIds.length > 0 ? 1 : 2;
    if (activePartitions.length < minRequired) {
        addMessage('Sposta almeno un gruppo di articoli nel Nuovo Modello a destra prima di procedere.', 'error');
        return;
    }

    const body = document.getElementById('bp-rev-modal-body');
    const footer = document.getElementById('bp-rev-modal-footer');
    if (body) {
        body.innerHTML = `
            <div style="padding: 56px 24px; text-align: center; color: var(--text-secondary);">
                <div class="spinner" style="margin: 0 auto 16px auto;"></div>
                <h4 style="margin: 0 0 6px 0; color: var(--text-primary);">
                    Generazione nuove schede da zero tramite AI...
                </h4>
                <div style="font-size: 13px; color: var(--text-muted);">
                    Compilazione di nomi, descrizioni e tag per i nuovi Blueprint mantenendo le foto selezionate.
                </div>
            </div>
        `;
    }
    if (footer) footer.innerHTML = '';

    try {
        const res = await api.prepareManualBlueprintSplit({
            blueprint_id: _currentContext.blueprint_id,
            source_blueprint_ids: _relatedBlueprintIds,
            partitions: activePartitions,
        });
        _currentJobId = res.job_id;
        _currentStagingJob = res.data;
        document.getElementById('modal-blueprint-revision')?.classList.remove('active', 'open');
        _renderStep3BeforeAfterStaging();
    } catch (err) {
        addMessage(`Errore nella preparazione della Staging Area: ${err.message}`, 'error');
        _renderStep1BucketSelector();
    }
}

async function _handleDeduceRule(userCorrection = null) {
    const activePartitions = _collectActivePartitions();

    if (activePartitions.length < 2) {
        addMessage('Sposta almeno un gruppo di articoli nel Nuovo Modello a destra prima di procedere.', 'error');
        return;
    }

    const body = document.getElementById('bp-rev-modal-body');
    const footer = document.getElementById('bp-rev-modal-footer');
    if (body) {
        body.innerHTML = `
            <div style="padding: 56px 24px; text-align: center; color: var(--text-secondary);">
                <div class="spinner" style="margin: 0 auto 16px auto;"></div>
                <h4 style="margin: 0 0 6px 0; color: var(--text-primary);">
                    ${userCorrection ? 'Ricalcolo della regola in base alle tue indicazioni...' : 'Analisi dei codici fornitore e deduzione della regola...'}
                </h4>
                <div style="font-size: 13px; color: var(--text-muted);">
                    L'AI sta verificando la separazione richiesta su tutti gli articoli del brand <strong>${_esc(_currentContext.brand_name)}</strong>.
                </div>
            </div>
        `;
    }
    if (footer) footer.innerHTML = '';

    try {
        const res = await api.deduceBlueprintRule({
            blueprint_id: _currentContext.blueprint_id,
            job_id: _currentJobId,
            partitions: activePartitions,
            user_correction: userCorrection,
        });
        _currentJobId = res.job_id;
        _currentRuleResponse = res;
        _renderStep2RuleReview();
    } catch (err) {
        addMessage(`Errore durante l'elaborazione della regola: ${err.message}`, 'error');
        if (_currentRuleResponse) {
            _renderStep2RuleReview();
        } else {
            _renderStep1BucketSelector();
        }
    }
}

// ===========================================================================
// STEP 2: Natural Language Rule Explanation & Correction Loop
// ===========================================================================

function _renderStep2RuleReview() {
    _renderStepIndicator(2);

    const body = document.getElementById('bp-rev-modal-body');
    const footer = document.getElementById('bp-rev-modal-footer');
    if (!body || !footer || !_currentRuleResponse) return;

    const r = _currentRuleResponse;
    const examplesRows = (r.examples || [])
        .map(
            (ex) => `
            <tr>
                <td style="font-family: monospace; font-weight: 600; color: var(--text-primary);">${_esc(ex.raw)}</td>
                <td style="color: var(--text-muted);">➔</td>
                <td>
                    <span class="bp-rev-code-pill">${_esc(ex.normalized)}</span>
                </td>
            </tr>
        `
        )
        .join('');

    body.innerHTML = `
        <div class="bp-rev-rule-review-wrap">
            <!-- Natural Language Explanation Card -->
            <div class="bp-rev-explanation-card">
                <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 10px;">
                    <span class="material-symbols-rounded" style="color: var(--accent);">psychology</span>
                    <span style="font-size: 13px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; color: var(--accent);">
                        Nuova Regola Elaborata per ${_esc(_currentContext.brand_name)}
                    </span>
                </div>
                <p class="bp-rev-explanation-text">${_esc(r.textual_explanation)}</p>
            </div>

            <!-- Concrete Examples & Warehouse Impact Grid -->
            <div style="display: grid; grid-template-columns: 1.2fr 1fr; gap: 16px; margin-top: 16px;">
                <div class="bp-rev-panel-box">
                    <h4 class="bp-rev-box-title">Esempi Pratici di Classificazione</h4>
                    <p style="font-size: 12px; color: var(--text-muted); margin: 0 0 10px 0;">
                        Il simbolo <strong>#</strong> indica la parte del codice che identifica il colore (ignorata per distinguere i modelli):
                    </p>
                    <table class="bp-rev-examples-table">
                        <thead>
                            <tr>
                                <th>Codice Fornitore</th>
                                <th></th>
                                <th>Modello Riconosciuto</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${examplesRows}
                        </tbody>
                    </table>
                </div>

                <div class="bp-rev-panel-box">
                    <h4 class="bp-rev-box-title">Anomalie Rilevate sul Magazzino</h4>
                    <p style="font-size: 12px; color: var(--text-muted); margin: 0 0 12px 0;">
                        In Staging Area verrà eseguita subito la separazione della scheda corrente. Eventuali altre schede anomale verranno segnalate nel pulsante <strong>Anomalie Magazzino</strong> per la revisione manuale una ad una:
                    </p>
                    <div class="bp-rev-impact-stats">
                        <div class="bp-rev-impact-item">
                            <span class="material-symbols-rounded" style="color: var(--accent);">call_split</span>
                            <div>
                                <strong>${r.estimated_splits_count}</strong> schede con articoli da dividere
                            </div>
                        </div>
                        <div class="bp-rev-impact-item">
                            <span class="material-symbols-rounded" style="color: #81c995;">merge</span>
                            <div>
                                <strong>${r.estimated_merges_count}</strong> schede duplicate rilevabili
                            </div>
                        </div>
                        <div class="bp-rev-impact-item">
                            <span class="material-symbols-rounded" style="color: #fdd663;">sync</span>
                            <div>
                                <strong>${r.aligned_blueprints_count}</strong> codici modello già corretti che verranno riallineati
                            </div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Interactive Correction Loop -->
            <div class="bp-rev-correction-box">
                <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 6px;">
                    <span class="material-symbols-rounded" style="font-size: 18px; color: var(--text-secondary);">chat</span>
                    <label for="bp-rev-correction-input" style="font-size: 13px; font-weight: 600; color: var(--text-primary);">
                        La spiegazione dell'AI contiene un errore? Correggila qui:
                    </label>
                </div>
                <div style="display: flex; gap: 10px; align-items: flex-start;">
                    <textarea
                        id="bp-rev-correction-input"
                        class="form-control"
                        rows="2"
                        placeholder="Es. No, i primi 6 caratteri indicano la linea/modello mentre gli ultimi 3 caratteri indicano il colore..."
                        style="flex: 1; font-size: 13px;"
                    ></textarea>
                    <button type="button" id="btn-bp-rev-recalculate" class="btn-secondary" style="white-space: nowrap; height: 54px; padding: 0 16px; display: inline-flex; align-items: center; gap: 6px;">
                        <span class="material-symbols-rounded" style="font-size: 18px;">refresh</span>
                        Ricalcola Regola
                    </button>
                </div>
            </div>
        </div>
    `;

    footer.innerHTML = `
        <button type="button" id="btn-bp-rev-back-step1" class="btn-secondary" style="display: inline-flex; align-items: center; gap: 6px;">
            <span class="material-symbols-rounded" style="font-size: 18px;">arrow_back</span>
            Modifica Separazione Articoli
        </button>
        <button type="button" id="btn-bp-rev-confirm-rule" class="btn-primary" style="display: inline-flex; align-items: center; gap: 6px;">
            <span class="material-symbols-rounded" style="font-size: 18px;">check_circle</span>
            Conferma Regola e Vai a Staging
        </button>
    `;

    document.getElementById('btn-bp-rev-back-step1')?.addEventListener('click', () => {
        _renderStep1BucketSelector();
    });

    document.getElementById('btn-bp-rev-recalculate')?.addEventListener('click', async () => {
        const input = document.getElementById('bp-rev-correction-input');
        const feedback = input ? input.value.trim() : '';
        if (!feedback) {
            addMessage('Inserisci una correzione testuale prima di chiedere il ricalcolo.', 'error');
            return;
        }
        await _handleDeduceRule(feedback);
    });

    document.getElementById('btn-bp-rev-confirm-rule')?.addEventListener('click', async () => {
        await _handlePrepareStagingPlan();
    });
}

async function _handlePrepareStagingPlan() {
    if (!_currentJobId) return;

    const body = document.getElementById('bp-rev-modal-body');
    const footer = document.getElementById('bp-rev-modal-footer');
    if (body) {
        body.innerHTML = `
            <div style="padding: 56px 24px; text-align: center; color: var(--text-secondary);">
                <div class="spinner" style="margin: 0 auto 16px auto;"></div>
                <h4 style="margin: 0 0 6px 0; color: var(--text-primary);">
                    Ricreazione schede da zero tramite AI e preparazione vista "Prima e Dopo"...
                </h4>
                <div style="font-size: 13px; color: var(--text-muted);">
                    Compilazione automatica di nomi, descrizioni e tag per i nuovi Blueprint mantenendo le foto associate.
                </div>
            </div>
        `;
    }
    if (footer) footer.innerHTML = '';

    try {
        const jobRes = await api.prepareBlueprintStagingPlan(_currentJobId);
        _currentStagingJob = jobRes.data;
        // Close the modal and open the Split-Screen Staging Area Before/After view
        document.getElementById('modal-blueprint-revision')?.classList.remove('active', 'open');
        _renderStep3BeforeAfterStaging();
    } catch (err) {
        addMessage(`Errore nella preparazione della Staging Area: ${err.message}`, 'error');
        _renderStep2RuleReview();
    }
}

// ===========================================================================
// STEP 3: Split-Screen Staging Area "Prima e Dopo" (Single Blueprint Scope)
// ===========================================================================

function _renderStep3BeforeAfterStaging() {
    enterSplitScreen();
    const panel = getDetailPanel();
    if (!panel || !_currentStagingJob) return;

    const plan = _currentStagingJob.reconciliation_plan || {};
    const targetSplit = plan.target_split;
    const alignedBlueprints = plan.aligned_blueprints || [];
    const isManualOnly = Boolean(_currentStagingJob.skip_rule_update);
    const ruleExplanation = _currentStagingJob.heuristic_state?.textual_explanation || '';

    const bannerHtml = isManualOnly
        ? `
            <div class="bp-rev-explanation-card" style="padding:12px 16px;">
                <div style="font-size:11px;font-weight:700;text-transform:uppercase;color:var(--accent);margin-bottom:4px;">
                    Correzione Manuale Singola Scheda (Regola Brand Invariata)
                </div>
                <div style="font-size:13px;color:var(--text-primary);line-height:1.45;">
                    Tutte le nuove schede nella colonna <strong>DOPO</strong> vengono ricreate da zero mantenendo le foto selezionate. La regola di riconoscimento del brand non verrà ricalcolata.
                </div>
            </div>
        `
        : `
            <div class="bp-rev-explanation-card" style="padding:12px 16px;">
                <div style="font-size:11px;font-weight:700;text-transform:uppercase;color:var(--accent);margin-bottom:4px;">
                    Regola Attiva Confermata
                </div>
                <div style="font-size:13px;color:var(--text-primary);line-height:1.45;">
                    ${_esc(ruleExplanation)}
                </div>
                <div style="font-size:12px;color:var(--text-muted);margin-top:6px;">
                    Oltre alla creazione da zero delle nuove schede qui sotto, verranno allineati i codici di <strong>${alignedBlueprints.length}</strong> schede già corrette. Eventuali altre schede anomale in magazzino saranno accessibili dal pulsante <strong>Anomalie Magazzino</strong>.
                </div>
            </div>
        `;

    panel.innerHTML = `
        <div class="detail-panel__header">
            <div class="detail-panel__header-title-box">
                <h2 class="detail-panel__title" style="display:flex;align-items:center;gap:8px;">
                    <span class="material-symbols-rounded" style="color:var(--accent);">compare_arrows</span>
                    Revisione Prima e Dopo (${_esc(_currentStagingJob.brand_name || '')})
                </h2>
                <span class="detail-panel__count" style="font-size:12px;color:var(--text-muted)">
                    Verifica le nuove schede ricreate da zero e le relative foto prima del salvataggio
                </span>
            </div>
            <div class="detail-panel__actions" style="display:flex;gap:8px;align-items:center;">
                <button type="button" id="btn-bp-staging-reopen-rule" class="btn-secondary" style="padding:6px 12px;font-size:12px;">
                    ${isManualOnly ? 'Modifica Separazione' : 'Rivedi Regola'}
                </button>
                <button type="button" id="btn-bp-staging-commit" class="btn-primary" style="padding:7px 16px;font-size:13px;display:inline-flex;align-items:center;gap:6px;">
                    <span class="material-symbols-rounded" style="font-size:17px;">done_all</span>
                    Conferma e Salva Schede
                </button>
                <button type="button" id="btn-bp-staging-close" class="btn-close-panel" title="Chiudi">
                    <span class="material-symbols-rounded">close</span>
                </button>
            </div>
        </div>

        <div id="bp-staging-scroll-body" style="flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:20px;">
            ${bannerHtml}

            <!-- Single Split Blueprint (Prima e Dopo) -->
            ${targetSplit ? _renderSplitBeforeAfterCardHtml(targetSplit, 'Separazione Scheda Articolo', true) : ''}
        </div>
    `;

    // Bind Staging Header Buttons
    document.getElementById('btn-bp-staging-close')?.addEventListener('click', () => {
        exitSplitScreen();
    });

    document.getElementById('btn-bp-staging-reopen-rule')?.addEventListener('click', () => {
        const modal = document.getElementById('modal-blueprint-revision');
        if (modal) modal.classList.add('active', 'open');
        if (isManualOnly) {
            _renderStep1BucketSelector();
        } else {
            _renderStep2RuleReview();
        }
    });

    // Bind inline save buttons for edited blueprint cards
    panel.querySelectorAll('.btn-bp-save-inline-card').forEach((btn) => {
        btn.addEventListener('click', async (e) => {
            const cardEl = e.currentTarget.closest('.bp-after-editable-card');
            if (!cardEl) return;
            await _handleSaveInlineCardEdit(cardEl);
        });
    });

    // Bind Photo Controls & Carousel
    _bindPhotoColEvents(panel);

    // Bind Final Atomic Commit
    document.getElementById('btn-bp-staging-commit')?.addEventListener('click', async (e) => {
        const btn = e.currentTarget;
        btn.disabled = true;
        btn.innerHTML = `<span class="spinner" style="width:14px;height:14px;border-width:2px;"></span> Salvataggio DB...`;
        try {
            await _saveAllInlineCardEdits(panel);
            const res = await api.commitBlueprintReconciliationPlan(_currentJobId);
            const remainingMsg =
                res.remaining_anomalies > 0
                    ? ` (${res.remaining_anomalies} altre anomalie segnalate in magazzino)`
                    : '';
            addMessage(
                `Operazione completata: ${res.splits_created || 0} nuove schede ricreate da zero${remainingMsg}.`,
                'system'
            );
            exitSplitScreen();
            await refreshWarehouseAnomaliesBadge();
            if (typeof _onCompleteCallback === 'function') {
                await _onCompleteCallback();
            }
        } catch (err) {
            btn.disabled = false;
            btn.innerHTML = `<span class="material-symbols-rounded" style="font-size:17px;">done_all</span> Conferma e Salva Schede`;
            addMessage(`Errore durante il salvataggio definitivo: ${err.message}`, 'error');
        }
    });
}

function _renderPhotoColHtml(targetId, articleGroups = [], articleName = '') {
    if (!articleGroups || articleGroups.length === 0) {
        return `
            <div class="blueprint-card__photo-col" data-target-id="${_esc(targetId)}" style="width:140px;display:flex;flex-direction:column;flex-shrink:0;">
                <div class="photo-placeholder" style="width:140px;height:140px;border-radius:var(--radius-sm);background:var(--bg-elevated);display:flex;align-items:center;justify-content:center;color:var(--text-muted);font-size:36px;">🖼</div>
            </div>
        `;
    }

    let currentIndex = _activeAfterPhotoIndices[targetId] || 0;
    if (currentIndex >= articleGroups.length || currentIndex < 0) {
        currentIndex = 0;
        _activeAfterPhotoIndices[targetId] = 0;
    }

    const currentGroup = articleGroups[currentIndex] || articleGroups[0];
    const photoUrl = currentGroup.photo_url || '';
    const colorLabel = (currentGroup.colors || []).join(', ') || 'N/D';
    const brandName = _currentStagingJob?.brand_name || '';
    const searchQuery = `${brandName} ${articleName || ''} ${currentGroup.supplier_code || ''} ${colorLabel}`.trim();

    const imgHtml = photoUrl
        ? `<img class="bp-photo-preview-img" src="${_esc(photoUrl)}" alt="foto" style="width:140px;height:140px;border-radius:var(--radius-sm);object-fit:cover;cursor:pointer;" onclick="window._openImageModal && window._openImageModal(this.src)" onerror="this.style.display='none'">`
        : `<div class="photo-placeholder" style="width:140px;height:140px;border-radius:var(--radius-sm);background:var(--bg-elevated);display:flex;align-items:center;justify-content:center;color:var(--text-muted);font-size:36px;cursor:pointer;">🖼</div>`;

    let navHtml = '';
    if (articleGroups.length > 1) {
        navHtml = `
            <div class="photo-carousel-nav" style="display:flex;justify-content:space-between;align-items:center;margin:6px 0;width:100%;">
                <button type="button" class="btn-prev-after-photo" data-target-id="${_esc(targetId)}" style="background:var(--bg-elevated);border:1px solid var(--border-color);border-radius:4px;cursor:pointer;padding:2px 8px;color:var(--text-primary);font-size:12px;">◀</button>
                <span style="font-size:11px;font-weight:600;color:var(--text-secondary);text-align:center;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding:0 4px;" title="${_esc(colorLabel)}">
                    ${_esc(colorLabel)}<br><span style="font-weight:normal;font-size:10px;">(${currentIndex + 1}/${articleGroups.length})</span>
                </span>
                <button type="button" class="btn-next-after-photo" data-target-id="${_esc(targetId)}" style="background:var(--bg-elevated);border:1px solid var(--border-color);border-radius:4px;cursor:pointer;padding:2px 8px;color:var(--text-primary);font-size:12px;">▶</button>
            </div>
        `;
    }

    return `
        <div class="blueprint-card__photo-col" data-target-id="${_esc(targetId)}" style="width:140px;display:flex;flex-direction:column;flex-shrink:0;">
            <div style="width:140px;height:140px;display:flex;align-items:center;justify-content:center;background:var(--bg-elevated);border-radius:var(--radius-sm);overflow:hidden;">
                ${imgHtml}
            </div>
            ${navHtml}
            <button type="button" class="btn-search-photo btn-secondary" data-target-id="${_esc(targetId)}" data-search-query="${_esc(searchQuery)}" style="margin-top:${articleGroups.length > 1 ? '4px' : '8px'};padding:4px 8px;font-size:11px;width:100%;" title="Cerca su Google">🔍 Cerca</button>
            <button type="button" class="btn-upload-photo btn-secondary" data-target-id="${_esc(targetId)}" data-color="${_esc(colorLabel)}" style="margin-top:4px;padding:4px 8px;font-size:11px;width:100%;" title="Carica foto dal dispositivo">📁 Carica Foto</button>
            <button type="button" class="btn-manual-photo btn-secondary" data-target-id="${_esc(targetId)}" data-color="${_esc(colorLabel)}" style="margin-top:4px;padding:4px 8px;font-size:11px;width:100%;">🔗 Inserisci URL</button>
            <button type="button" class="btn-retry-photo btn-secondary" data-target-id="${_esc(targetId)}" data-color="${_esc(colorLabel)}" style="margin-top:4px;padding:4px 8px;font-size:11px;width:100%;">🔄 Riprova</button>
        </div>
    `;
}

function _renderSplitBeforeAfterCardHtml(splitOp, badgeTitle, isPrimary) {
    const before = splitOp.before || {};
    const afterList = splitOp.after || [];

    const beforeGroups = before.article_groups || [];
    const totalBeforeUnits = beforeGroups.reduce((s, g) => s + (g.quantity || 0), 0);
    const beforeArticlesHtml = beforeGroups
        .map(
            (g) => `
            <span class="bp-mini-art-chip">
                <strong>${_esc(g.supplier_code)}</strong> (${_esc((g.colors || []).join(', ') || 'N/D')} • x${g.quantity || 1})
            </span>
        `
        )
        .join('');

    const beforePhotosHtml =
        beforeGroups.length > 0
            ? `
        <div class="bp-before-photos-strip" style="display:flex;gap:6px;margin:8px 0 10px 0;overflow-x:auto;padding-bottom:4px;">
            ${beforeGroups
                .map(
                    (g) => `
                <div class="bp-mini-photo-card" style="width:60px;height:60px;border-radius:4px;background:var(--bg-elevated);overflow:hidden;flex-shrink:0;cursor:pointer;position:relative;" onclick="window._openImageModal && window._openImageModal('${_esc(g.photo_url || '')}')" title="${_esc(g.supplier_code)} (${_esc((g.colors || []).join(', '))})">
                    ${g.photo_url ? `<img src="${_esc(g.photo_url)}" style="width:100%;height:100%;object-fit:cover;" alt="foto">` : `<span style="display:flex;align-items:center;justify-content:center;height:100%;font-size:22px;color:var(--text-muted);">🖼</span>`}
                    <span style="position:absolute;bottom:0;left:0;right:0;background:rgba(0,0,0,0.65);color:#fff;font-size:8.5px;text-align:center;padding:1px 2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${_esc((g.colors || [])[0] || g.supplier_code)}</span>
                </div>
            `
                )
                .join('')}
        </div>
    `
            : '';

    const afterCardsHtml = afterList
        .map((abp, idx) => {
            const catText = abp.category?.description || '';
            const subCatText = abp.sub_category?.description || '';
            const fullCatDisplay = subCatText ? `${catText} ↳ ${subCatText}` : catText;
            return `
            <div class="bp-after-editable-card" data-target-id="${_esc(abp.id)}" style="background:var(--bg-surface);border:1px solid var(--border-color);border-radius:var(--radius-card);padding:14px;box-shadow:var(--shadow-sm);">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
                    <span class="bp-rev-badge bp-rev-badge--new">
                        Nuova Scheda #${idx + 1} Ricreata da Zero
                    </span>
                    <span class="bp-rev-code-pill" title="Codice Modello Normalizzato">
                        Modello: ${_esc(abp.normalized_vendor_code || '')}
                    </span>
                </div>

                <div class="bp-after-card-body" style="display:flex;gap:16px;align-items:flex-start;">
                    ${_renderPhotoColHtml(abp.id, abp.article_groups, abp.article_name)}

                    <div style="flex:1;min-width:0;">
                        <div class="bp-inline-form-grid">
                            <div>
                                <label>Nome Articolo</label>
                                <input type="text" class="form-control bp-inline-input" data-field="article_name" value="${_esc(abp.article_name || '')}">
                            </div>
                            <div>
                                <label>Categoria / Sotto-categoria</label>
                                <input type="text" class="form-control" value="${_esc(fullCatDisplay)}" disabled>
                            </div>
                        </div>

                        <div style="margin-top:8px;">
                            <label style="font-size:11px;color:var(--text-muted);">Descrizione Breve</label>
                            <textarea class="form-control bp-inline-input" data-field="description" rows="2" style="font-size:12.5px;">${_esc(abp.description || '')}</textarea>
                        </div>

                        <div class="bp-inline-form-grid" style="margin-top:8px;">
                            <div>
                                <label>Tags (separati da virgola)</label>
                                <input type="text" class="form-control bp-inline-input" data-field="tags" value="${_esc((abp.tags || []).join(', '))}">
                            </div>
                            <div>
                                <label>Materiali (separati da virgola)</label>
                                <input type="text" class="form-control bp-inline-input" data-field="materials" value="${_esc((abp.materials || []).join(', '))}">
                            </div>
                        </div>

                        <div style="margin-top:10px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
                            <div style="display:flex;flex-wrap:wrap;gap:4px;">
                                ${(abp.article_groups || [])
                                    .map(
                                        (g) => `
                                        <span class="bp-mini-art-chip bp-mini-art-chip--after">
                                            <strong>${_esc(g.supplier_code)}</strong> (${_esc((g.colors || []).join(', ') || 'N/D')} • x${g.quantity || 1})
                                        </span>
                                    `
                                    )
                                    .join('')}
                            </div>
                            <button type="button" class="btn-secondary btn-bp-save-inline-card" style="padding:4px 10px;font-size:11.5px;">
                                Aggiorna Scheda
                            </button>
                        </div>
                    </div>
                </div>
            </div>
        `;
        })
        .join('');

    return `
        <div class="bp-before-after-block ${isPrimary ? 'bp-before-after-block--primary' : ''}">
            <div class="bp-before-after-block__header">
                <span class="bp-rev-badge bp-rev-badge--orig">${_esc(badgeTitle)}</span>
                <span style="font-size:13px;font-weight:600;color:var(--text-primary);">${_esc(before.article_name || '')}</span>
            </div>
            <div class="bp-before-after-grid">
                <!-- PRIMA -->
                <div class="bp-ba-col bp-ba-col--before">
                    <div class="bp-ba-col__label">PRIMA (Scheda Precedente da Sostituire)</div>
                    <div class="bp-before-summary-card">
                        <div style="font-weight:600;font-size:14px;color:var(--text-primary);margin-bottom:4px;">
                            ${_esc(before.article_name || '')}
                        </div>
                        <div style="font-size:12px;color:var(--text-muted);margin-bottom:8px;">
                            ${_esc(before.description || '')}
                        </div>
                        ${beforePhotosHtml}
                        <div style="font-size:11.5px;color:var(--text-secondary);margin-bottom:6px;">
                            Articoli fisici presenti (${totalBeforeUnits} unità):
                        </div>
                        <div style="display:flex;flex-wrap:wrap;gap:4px;">
                            ${beforeArticlesHtml}
                        </div>
                    </div>
                </div>

                <!-- DOPO -->
                <div class="bp-ba-col bp-ba-col--after">
                    <div class="bp-ba-col__label">DOPO (${afterList.length} ${afterList.length === 1 ? 'Nuova Scheda Ricreata' : 'Nuove Schede Distinte'})</div>
                    <div style="display:flex;flex-direction:column;gap:12px;">
                        ${afterCardsHtml}
                    </div>
                </div>
            </div>
        </div>
    `;
}

function _bindPhotoColEvents(container) {
    if (!container) return;

    const findCard = (targetId) => {
        const plan = _currentStagingJob?.reconciliation_plan || {};
        for (const bp of plan.target_split?.after || []) {
            if (bp.id === targetId) return bp;
        }
        return null;
    };

    // Carousel buttons
    container.querySelectorAll('.btn-prev-after-photo').forEach((btn) => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const targetId = btn.dataset.targetId;
            const card = findCard(targetId);
            if (!card || !card.article_groups) return;
            const len = card.article_groups.length;
            let idx = _activeAfterPhotoIndices[targetId] || 0;
            idx = (idx - 1 + len) % len;
            _activeAfterPhotoIndices[targetId] = idx;
            _refreshPhotoCol(targetId, card);
        });
    });

    container.querySelectorAll('.btn-next-after-photo').forEach((btn) => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const targetId = btn.dataset.targetId;
            const card = findCard(targetId);
            if (!card || !card.article_groups) return;
            const len = card.article_groups.length;
            let idx = _activeAfterPhotoIndices[targetId] || 0;
            idx = (idx + 1) % len;
            _activeAfterPhotoIndices[targetId] = idx;
            _refreshPhotoCol(targetId, card);
        });
    });

    // 🔍 Cerca (Google Images)
    container.querySelectorAll('.btn-search-photo').forEach((btn) => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const query = btn.dataset.searchQuery;
            if (!query) {
                addMessage('Nessuna informazione sufficiente per la ricerca.', 'error');
                return;
            }
            const searchUrl = `https://www.google.com/search?tbm=isch&q=${encodeURIComponent(query)}`;
            window.open(searchUrl, '_blank', 'noopener,noreferrer');
        });
    });

    // 📁 Carica Foto
    container.querySelectorAll('.btn-upload-photo').forEach((btn) => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const targetId = btn.dataset.targetId;
            const color = btn.dataset.color || '';
            const card = findCard(targetId);
            if (!card || !_currentJobId) return;

            requestPhotoUpload({
                onSuccess: async (compressedBlob) => {
                    addMessage('Caricamento foto in corso...', 'system');
                    btn.disabled = true;
                    try {
                        const res = await api.uploadBlueprintRevisionPhoto(_currentJobId, targetId, color, compressedBlob);
                        if (res && res.photo_url) {
                            _applyPhotoUpdateToCard(card, targetId, color, res.photo_url);
                            _refreshPhotoCol(targetId, card);
                            addMessage('Foto caricata con successo!', 'system');
                        }
                    } catch (err) {
                        addMessage(`Upload foto fallito: ${err.message}`, 'error');
                    } finally {
                        btn.disabled = false;
                    }
                },
                onError: (err) => {
                    addMessage(`Errore selezione foto: ${err.message || err}`, 'error');
                },
            });
        });
    });

    // 🔗 Inserisci URL
    container.querySelectorAll('.btn-manual-photo').forEach((btn) => {
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            const targetId = btn.dataset.targetId;
            const color = btn.dataset.color || '';
            const card = findCard(targetId);
            if (!card || !_currentJobId) return;

            const url = prompt("Inserisci l'URL diretto dell'immagine:");
            if (!url || !url.trim()) return;

            btn.disabled = true;
            btn.textContent = '...';
            try {
                const res = await api.retryBlueprintRevisionPhoto(_currentJobId, {
                    target_blueprint_id: targetId,
                    color: color,
                    new_url: url.trim(),
                });
                if (res && res.photo_url) {
                    _applyPhotoUpdateToCard(card, targetId, color, res.photo_url);
                    _refreshPhotoCol(targetId, card);
                    addMessage("Foto aggiornata dall'URL fornito!", 'system');
                }
            } catch (err) {
                addMessage(`Errore nell'impostazione dell'URL: ${err.message}`, 'error');
            } finally {
                btn.disabled = false;
                btn.textContent = '🔗 Inserisci URL';
            }
        });
    });

    // 🔄 Riprova (AI Grounded Search)
    container.querySelectorAll('.btn-retry-photo').forEach((btn) => {
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            const targetId = btn.dataset.targetId;
            const color = btn.dataset.color || '';
            const card = findCard(targetId);
            if (!card || !_currentJobId) return;

            const feedback = prompt('Cosa non va nella foto attuale? (opzionale, aiuta la ricerca AI):');

            btn.disabled = true;
            btn.textContent = '...';
            addMessage('Ricerca foto su Google con AI in corso...', 'system');
            try {
                const res = await api.retryBlueprintRevisionPhoto(_currentJobId, {
                    target_blueprint_id: targetId,
                    color: color,
                    user_feedback: feedback ? feedback.trim() : null,
                });
                if (res && res.photo_url) {
                    _applyPhotoUpdateToCard(card, targetId, color, res.photo_url);
                    _refreshPhotoCol(targetId, card);
                    addMessage("Nuova foto trovata con successo dall'AI!", 'system');
                }
            } catch (err) {
                addMessage(`Ricerca AI non riuscita: ${err.message}`, 'error');
            } finally {
                btn.disabled = false;
                btn.textContent = '🔄 Riprova';
            }
        });
    });
}

function _applyPhotoUpdateToCard(card, targetId, color, newPhotoUrl) {
    const normColor = (color || '').trim().toLowerCase();
    const artGroups = card.article_groups || [];
    let curIdx = _activeAfterPhotoIndices[targetId] || 0;
    if (artGroups[curIdx]) {
        artGroups[curIdx].photo_url = newPhotoUrl;
    }
    for (const g of artGroups) {
        const cList = (g.colors || []).map((c) => String(c).trim().toLowerCase());
        if (!normColor || cList.includes(normColor) || artGroups.length === 1) {
            g.photo_url = newPhotoUrl;
        }
    }
}

function _refreshPhotoCol(targetId, card) {
    const panel = getDetailPanel();
    if (!panel) return;
    const oldCol = panel.querySelector(`.blueprint-card__photo-col[data-target-id="${targetId}"]`);
    if (!oldCol) return;
    const tempDiv = document.createElement('div');
    tempDiv.innerHTML = _renderPhotoColHtml(targetId, card.article_groups, card.article_name);
    const newCol = tempDiv.firstElementChild;
    oldCol.replaceWith(newCol);
    _bindPhotoColEvents(newCol);
}

function _collectCardUpdates(cardEl) {
    const targetId = cardEl.dataset.targetId;
    if (!targetId) return null;

    const getVal = (field) => {
        const el = cardEl.querySelector(`.bp-inline-input[data-field="${field}"]`);
        return el ? el.value.trim() : undefined;
    };

    const splitCsv = (val) =>
        val !== undefined
            ? val
                  .split(',')
                  .map((s) => s.trim())
                  .filter(Boolean)
            : undefined;

    return {
        target_blueprint_id: targetId,
        article_name: getVal('article_name'),
        description: getVal('description'),
        tags: splitCsv(getVal('tags')),
        materials: splitCsv(getVal('materials')),
    };
}

async function _handleSaveInlineCardEdit(cardEl) {
    const payload = _collectCardUpdates(cardEl);
    if (!payload || !_currentJobId) return;

    try {
        const res = await api.updateBlueprintReconciliationPlan(_currentJobId, payload);
        _currentStagingJob = res.data;
        addMessage('Scheda aggiornata.', 'system');
    } catch (err) {
        addMessage(`Errore nel salvataggio della scheda: ${err.message}`, 'error');
    }
}

async function _saveAllInlineCardEdits(panel) {
    if (!panel || !_currentJobId) return;
    const cards = panel.querySelectorAll('.bp-after-editable-card');
    for (const cardEl of cards) {
        const payload = _collectCardUpdates(cardEl);
        if (payload) {
            await api.updateBlueprintReconciliationPlan(_currentJobId, payload);
        }
    }
}
