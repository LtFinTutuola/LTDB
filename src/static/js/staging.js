/**
 * staging.js
 * Manages the staging split-screen panel:
 *  - Renders blueprint cards with click-to-edit fields
 *  - Maintains an operations queue with deduplication
 *  - Submits revision batches to PUT /ingestion/staging/{job_id}
 *  - Handles photo proposal display and retry
 */
import * as api from './api.js';
import {
    appState, Mode, setMode,
    addMessage, getDetailPanel, exitSplitScreen, _esc,
} from './main.js';

// ---- Module state ----
let _activePhotoIndices = {};
let _jobId = null;
let _stagingData = null;
let _operationsQueue = [];
let _categoryHierarchy = null;

// Fields treated as comma-separated arrays in the UI
const LIST_FIELDS = new Set(['colors', 'tags', 'materials']);

// ---- Public API ----

export async function renderStagingPanel(data, jobId) {
    _jobId = jobId;
    _stagingData = JSON.parse(JSON.stringify(data)); // deep copy
    _operationsQueue = [];
    
    if (_stagingData.brand_id && !_stagingData.brand_name) {
        try {
            const brands = await api.getBrands();
            const b = brands.find(x => x.id === _stagingData.brand_id);
            if (b) _stagingData.brand_name = b.name;
        } catch (e) {
            console.error("Failed to fetch brands", e);
        }
    }
    
    if (_stagingData.brand_id && !_categoryHierarchy) {
        try {
            const res = await api.getBrandCategories(_stagingData.brand_id);
            _categoryHierarchy = res.data || res;
        } catch (e) {
            console.error("Failed to fetch category hierarchy", e);
            _categoryHierarchy = {};
        }
    } else if (!_categoryHierarchy) {
        _categoryHierarchy = {};
    }

    _render();
}

export async function submitRevisions() {
    if (_operationsQueue.length === 0) {
        addMessage('Nessuna modifica da applicare.', 'system');
        return;
    }
    try {
        const result = await api.submitRevisions(_jobId, _operationsQueue);
        _operationsQueue = [];
        _stagingData = result.data;
        _render();
        addMessage(`✅ Modifiche applicate (${result.data?.items?.length ?? '?'} elementi).`, 'success');
    } catch (err) {
        addMessage(`❌ Errore: ${err.message}`, 'error');
    }
}

// ---- Rendering ----

function _render() {
    const panel = getDetailPanel();
    const grouped = _groupByBlueprint(_stagingData);
    const photoProposals = _stagingData?.photo_proposals || {};

    let html = `
        <div class="detail-panel__header">
            <div class="detail-panel__header-title-box">
                <h2 class="detail-panel__title">Revisione dati</h2>
                <span style="font-size:12px;color:var(--text-muted)">${(_stagingData?.items || []).length} elementi</span>
            </div>
            <div>
                <button class="btn-refresh-panel" title="Ricarica" style="background:none;border:none;cursor:pointer;font-size:16px;color:var(--text-secondary);margin-right:8px;">🔄</button>
                <button class="btn-close-panel" title="Chiudi">✕</button>
            </div>
        </div>
        <div class="detail-panel__content" id="staging-content" style="flex:1;overflow-y:auto;">
    `;

    if (grouped.size === 0) {
        html += `<div class="empty-state"><span class="empty-state__icon">📋</span><p class="empty-state__text">Nessun elemento da rivedere</p></div>`;
    } else {
        for (const [bpId, { blueprint, items }] of grouped) {
            html += _buildBlueprintCard(blueprint, items, photoProposals);
        }
    }

    html += '</div>';

    const hasChanges = _operationsQueue.length > 0;
    const btnText = hasChanges ? 'Applica modifiche' : '✅ Conferma e importa';
    const btnClass = hasChanges ? 'btn-apply-changes' : 'btn-confirm-import';

    html += `
        <div class="detail-panel__footer" style="padding: var(--space-md); border-top: 1px solid var(--border-color); display: flex; justify-content: flex-end; background: var(--bg-primary);">
            <button class="${btnClass} primary-btn" style="padding: 10px 20px; border-radius: var(--border-radius); border: none; font-weight: 600; cursor: pointer; background: ${hasChanges ? 'var(--warning)' : 'var(--success)'}; color: #fff;">${btnText}</button>
        </div>
    `;

    panel.innerHTML = html;
    
    const closeBtn = panel.querySelector('.btn-close-panel');
    if (closeBtn) closeBtn.addEventListener('click', exitSplitScreen);

    const refreshBtn = panel.querySelector('.btn-refresh-panel');
    if (refreshBtn) refreshBtn.addEventListener('click', _handleRefresh);
    
    const applyBtn = panel.querySelector('.btn-apply-changes');
    if (applyBtn) applyBtn.addEventListener('click', submitRevisions);

    const confirmBtn = panel.querySelector('.btn-confirm-import');
    if (confirmBtn) confirmBtn.addEventListener('click', _handleConfirm);

    _attachListeners();
}

function _groupByBlueprint(data) {
    const map = new Map();
    const bpMap = {};
    for (const bp of (data?.blueprints || [])) bpMap[bp.id] = bp;
    for (const item of (data?.items || [])) {
        const bpId = item.article_blueprint_id;
        if (!map.has(bpId)) map.set(bpId, { blueprint: bpMap[bpId] || { id: bpId, article_name: 'Blueprint ' + bpId.slice(0,8), description: '', tags: [], materials: [] }, items: [] });
        map.get(bpId).items.push(item);
    }
    return map;
}

function _buildBlueprintCard(bp, items, photoProposals) {
    const isNew = bp.is_new ? '<span class="badge-new">Nuovo</span>' : '';
    
    const photoHtml = _buildPhotoCarousel(bp.id, items, photoProposals);

    const tagsHtml = (bp.tags || []).map(t => `
        <span class="chip-tag">
            <span class="tag-text editable" data-entity="blueprint" data-id="${_esc(bp.id)}" data-field="tags" data-original-tag="${_esc(t)}">${_esc(t)}</span>
            <button class="btn-remove-tag" data-bp-id="${_esc(bp.id)}" data-tag="${_esc(t)}">✕</button>
        </span>
    `).join('');

    const catDesc = bp.category?.description || '';
    const subCatDesc = bp.sub_category?.description || '';
    
    const hasHierarchy = _categoryHierarchy && Object.keys(_categoryHierarchy).length > 0;
    
    let catOpts = '';
    let subCatOpts = '';
    if (hasHierarchy) {
        catOpts = Object.keys(_categoryHierarchy).map(c => `<option value="${_esc(c)}" ${catDesc === c ? 'selected' : ''}>${_esc(c)}</option>`).join('');
        const subCats = catDesc && _categoryHierarchy[catDesc] ? Object.keys(_categoryHierarchy[catDesc].sub_categories || {}) : [];
        subCatOpts = subCats.map(sc => `<option value="${_esc(sc)}" ${subCatDesc === sc ? 'selected' : ''}>${_esc(sc)}</option>`).join('');
    } else {
        if (catDesc) catOpts = `<option value="${_esc(catDesc)}" selected>${_esc(catDesc)}</option>`;
        if (subCatDesc) subCatOpts = `<option value="${_esc(subCatDesc)}" selected>${_esc(subCatDesc)}</option>`;
    }

    return `
    <div class="blueprint-card" data-bp-id="${_esc(bp.id)}">
        <div class="blueprint-card__row-top">
            ${photoHtml}
            <div class="blueprint-card__info-col">
                <div><span class="chip chip--small">${_esc(_stagingData?.brand_name || 'Brand')}</span></div>
                <div class="blueprint-card__name">
                    <span class="editable" data-entity="blueprint" data-id="${_esc(bp.id)}" data-field="article_name">${_esc(bp.article_name || '')}</span>${isNew}
                </div>
                <div>
                    <select class="category-select sel-macro-cat" data-bp-id="${_esc(bp.id)}" ${!hasHierarchy ? 'disabled' : ''}>
                        <option value="">Macro Categoria...</option>
                        ${catOpts}
                    </select>
                    <select class="category-select sel-sub-cat" data-bp-id="${_esc(bp.id)}" ${!hasHierarchy ? 'disabled' : ''}>
                        <option value="">Sotto Categoria...</option>
                        ${subCatOpts}
                    </select>
                </div>
                <div class="blueprint-card__desc">
                    <span class="editable" data-entity="blueprint" data-id="${_esc(bp.id)}" data-field="description">${_esc(bp.description || '')}</span>
                </div>
                <div class="blueprint-card__ext-desc">
                    <span class="editable" data-entity="blueprint" data-id="${_esc(bp.id)}" data-field="extended_description">${_esc(bp.extended_description || '')}</span>
                </div>
                <div class="blueprint-card__meta">
                    ${tagsHtml}
                    <span class="editable chip-tag" data-entity="blueprint" data-id="${_esc(bp.id)}" data-field="tags" style="background:transparent;border:1px dashed var(--border-color);padding:2px 8px;cursor:pointer;">+ Tag</span>
                </div>
            </div>
        </div>
        <div class="blueprint-card__row-bottom">
            ${_buildItemsTable(items, photoProposals, bp)}
        </div>
    </div>`;
}

function _buildItemsTable(items, photoProposals, bp) {
    let rows = items.map(item => {
        const colors = (item.colors || []).join(', ');
        const materials = (item.materials || bp.materials || []).join(', ');
        return `
        <tr data-item-id="${_esc(item.item_id)}">
            <td><span class="editable" data-entity="item" data-id="${_esc(item.item_id)}" data-field="vendor_code">${_esc(item.vendor_code || '')}</span></td>
            <td><span class="editable" data-entity="item" data-id="${_esc(item.item_id)}" data-field="barcode">${_esc(item.barcode || '')}</span></td>
            <td><span class="editable" data-entity="item" data-id="${_esc(item.item_id)}" data-field="quantity">${_esc(String(item.quantity ?? 1))}</span></td>
            <td><span class="editable" data-entity="item" data-id="${_esc(item.item_id)}" data-field="colors">${_esc(colors)}</span></td>
            <td><span class="editable" data-entity="blueprint" data-id="${_esc(bp.id)}" data-field="materials">${_esc(materials)}</span></td>
        </tr>
        `;
    }).join('');

    return `
    <table class="items-table">
        <thead><tr>
            <th>Codice</th><th>Barcode</th><th>Qtà</th><th>Colori</th><th>Materiali</th>
        </tr></thead>
        <tbody>${rows}</tbody>
    </table>`;
}

function _findProposal(photoProposals, itemId) {
    if (!photoProposals) return null;
    if (Array.isArray(photoProposals)) {
        return photoProposals.find(p => p.item_id === itemId) || null;
    }
    if (photoProposals[itemId]) return photoProposals[itemId];
    for (const val of Object.values(photoProposals)) {
        if (Array.isArray(val)) {
            const found = val.find(p => p.item_id === itemId);
            if (found) return found;
        }
    }
    return null;
}

function _buildPhotoCarousel(bpId, items, photoProposals) {
    if (!items || items.length === 0) {
        return `<div class="blueprint-card__photo-col"><div class="photo-placeholder" style="display:flex;align-items:center;justify-content:center;color:var(--text-muted);font-size:32px;">🖼</div></div>`;
    }

    let currentIndex = _activePhotoIndices[bpId] || 0;
    if (currentIndex >= items.length) currentIndex = 0;
    
    const currentItem = items[currentIndex];
    const proposal = _findProposal(photoProposals, currentItem.item_id) || {};
    const url = proposal.url || proposal.photo_url || null;
    
    const imgHtml = url
        ? `<img src="${_esc(url)}" alt="foto" onerror="this.style.display='none'" onclick="window._openImageModal(this.src)">`
        : `<div class="photo-placeholder" style="display:flex;align-items:center;justify-content:center;color:var(--text-muted);font-size:32px;">🖼</div>`;

    const colorsStr = (currentItem.colors || []).join(', ');
    const colorLabel = colorsStr || 'Nessun colore';
    
    let navHtml = '';
    if (items.length > 1) {
        navHtml = `
            <div class="photo-carousel-nav" style="display:flex;justify-content:space-between;align-items:center;margin-top:8px;">
                <button class="btn-prev-photo" data-bp-id="${_esc(bpId)}" style="background:var(--bg-tertiary);border:1px solid var(--border-color);border-radius:4px;cursor:pointer;padding:2px 8px;color:var(--text-primary);">◀</button>
                <span style="font-size:12px;font-weight:600;color:var(--text-secondary);text-align:center;flex:1;">${_esc(colorLabel)}<br><span style="font-weight:normal;">(${currentIndex + 1}/${items.length})</span></span>
                <button class="btn-next-photo" data-bp-id="${_esc(bpId)}" style="background:var(--bg-tertiary);border:1px solid var(--border-color);border-radius:4px;cursor:pointer;padding:2px 8px;color:var(--text-primary);">▶</button>
            </div>
        `;
    }

    return `
    <div class="blueprint-card__photo-col" style="display:flex;flex-direction:column;">
        ${imgHtml}
        ${navHtml}
        <button class="btn-retry-photo" data-item-id="${_esc(currentItem.item_id)}" data-job-id="${_esc(_jobId)}" style="margin-top:auto;">🔄 Riprova</button>
        <button class="btn-manual-photo" data-item-id="${_esc(currentItem.item_id)}" data-job-id="${_esc(_jobId)}" style="margin-top:4px;">🔗 Inserisci URL</button>
    </div>`;
}

// Global modal function
window._openImageModal = function(url) {
    let overlay = document.getElementById('image-zoom-modal');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'image-zoom-modal';
        overlay.className = 'image-modal-overlay';
        overlay.innerHTML = `
            <div class="image-modal-content">
                <button class="btn-close-modal">✕</button>
                <img id="image-zoom-img" src="" alt="zoom">
            </div>
        `;
        document.body.appendChild(overlay);
        
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay || e.target.classList.contains('btn-close-modal')) {
                overlay.classList.remove('open');
            }
        });
    }
    document.getElementById('image-zoom-img').src = url;
    overlay.classList.add('open');
};

// ---- Event listeners ----

function _attachListeners() {
    const content = document.getElementById('staging-content');
    if (!content) return;

    // Click-to-edit
    content.querySelectorAll('.editable').forEach(el => {
        el.addEventListener('click', _startEdit);
    });

    // Delete tag
    content.querySelectorAll('.btn-remove-tag').forEach(btn => {
        btn.addEventListener('click', _handleRemoveTag);
    });

    // Category changes
    content.querySelectorAll('.sel-macro-cat').forEach(sel => {
        sel.addEventListener('change', _handleMacroCatChange);
    });
    content.querySelectorAll('.sel-sub-cat').forEach(sel => {
        sel.addEventListener('change', _handleSubCatChange);
    });

    // Photo retry and manual url
    content.querySelectorAll('.btn-retry-photo').forEach(btn => {
        btn.addEventListener('click', _handleRetryPhoto);
    });
    content.querySelectorAll('.btn-manual-photo').forEach(btn => {
        btn.addEventListener('click', _handleManualPhoto);
    });

    // Carousel nav
    content.querySelectorAll('.btn-prev-photo').forEach(btn => {
        btn.addEventListener('click', (e) => {
            const bpId = e.currentTarget.dataset.bpId;
            const items = _stagingData.items.filter(i => i.article_blueprint_id === bpId);
            const len = items.length;
            if (len === 0) return;
            if (!_activePhotoIndices[bpId]) _activePhotoIndices[bpId] = 0;
            _activePhotoIndices[bpId] = (_activePhotoIndices[bpId] - 1 + len) % len;
            _render();
        });
    });
    content.querySelectorAll('.btn-next-photo').forEach(btn => {
        btn.addEventListener('click', (e) => {
            const bpId = e.currentTarget.dataset.bpId;
            const items = _stagingData.items.filter(i => i.article_blueprint_id === bpId);
            const len = items.length;
            if (len === 0) return;
            if (!_activePhotoIndices[bpId]) _activePhotoIndices[bpId] = 0;
            _activePhotoIndices[bpId] = (_activePhotoIndices[bpId] + 1) % len;
            _render();
        });
    });
}

function _handleRemoveTag(e) {
    const bpId = e.currentTarget.dataset.bpId;
    const tagToRemove = e.currentTarget.dataset.tag;
    const bp = _stagingData.blueprints.find(b => b.id === bpId);
    if (bp) {
        bp.tags = bp.tags.filter(t => t !== tagToRemove);
        _enqueueUpdate('blueprint', bpId, 'tags', bp.tags);
        _render(); // re-render to update UI
    }
}

function _handleMacroCatChange(e) {
    const sel = e.currentTarget;
    const bpId = sel.dataset.bpId;
    const val = sel.value;
    
    // Reset sub category
    _enqueueUpdate('blueprint', bpId, 'category', val ? { description: val } : null);
    _enqueueUpdate('blueprint', bpId, 'sub_category', null);
    
    // Update local data
    const bp = _stagingData.blueprints.find(b => b.id === bpId);
    if (bp) {
        bp.category = val ? { description: val } : null;
        bp.sub_category = null;
    }
    
    _render();
}

function _handleSubCatChange(e) {
    const sel = e.currentTarget;
    const bpId = sel.dataset.bpId;
    const val = sel.value;
    
    _enqueueUpdate('blueprint', bpId, 'sub_category', val ? { description: val } : null);
    
    const bp = _stagingData.blueprints.find(b => b.id === bpId);
    if (bp) {
        bp.sub_category = val ? { description: val } : null;
    }
}

function _startEdit(e) {
    const span = e.currentTarget;
    const entity = span.dataset.entity;
    const id     = span.dataset.id;
    const field  = span.dataset.field;
    
    let currentVal = span.textContent;
    let originalTag = span.dataset.originalTag || '';
    
    if (field === 'tags' && !originalTag) {
        currentVal = ''; // adding a new tag
    }

    const isMultiLine = field === 'extended_description';
    const input = document.createElement(isMultiLine ? 'textarea' : 'input');
    input.className = 'editable-input';
    input.value = currentVal;
    input.dataset.entity = entity;
    input.dataset.id     = id;
    input.dataset.field  = field;
    input.dataset.originalVal = originalTag || currentVal;
    input.dataset.isAddTag = field === 'tags' && !originalTag;
    input.dataset.isEditTag = field === 'tags' && !!originalTag;

    // Maintain dimensions during edit
    const isTableCell = span.parentElement.tagName === 'TD';
    if (!isTableCell) {
        input.style.width = Math.max(span.offsetWidth, 80) + 'px';
        if (isMultiLine) {
            input.style.height = Math.max(span.offsetHeight, 60) + 'px';
            input.style.resize = 'vertical';
        } else {
            input.style.height = Math.max(span.offsetHeight, 24) + 'px';
        }
    }

    span.replaceWith(input);
    input.focus();
    if (input.dataset.isAddTag !== 'true') input.select();

    input.addEventListener('blur',    () => _commitEdit(input));
    input.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter')  { ev.preventDefault(); input.blur(); }
        if (ev.key === 'Escape') { ev.preventDefault(); _cancelEdit(input); }
    });
}

function _commitEdit(input) {
    const entity   = input.dataset.entity;
    const id       = input.dataset.id;
    const field    = input.dataset.field;
    const newRaw   = input.value.trim();
    const origVal  = input.dataset.originalVal;
    const isAddTag = input.dataset.isAddTag === 'true';
    const isEditTag = input.dataset.isEditTag === 'true';

    if (!isAddTag && newRaw === origVal) {
        _render();
        return;
    }
    if (isAddTag && newRaw === '') {
        _render();
        return;
    }

    let value = _normalizeValue(field, newRaw);

    if (field === 'tags') {
        const bp = _stagingData.blueprints.find(b => b.id === id);
        if (bp) {
            if (isAddTag) {
                if (value.length > 0) {
                    bp.tags = [...(bp.tags || []), ...value];
                }
            } else if (isEditTag) {
                if (value.length === 0 || value[0] === '') {
                    bp.tags = bp.tags.filter(t => t !== origVal);
                } else {
                    const idx = bp.tags.indexOf(origVal);
                    if (idx > -1) {
                        bp.tags.splice(idx, 1, ...value);
                    } else {
                        bp.tags = [...(bp.tags || []), ...value];
                    }
                }
            }
            value = bp.tags;
        }
    }

    _enqueueUpdate(entity, id, field, value);

    if (field !== 'tags') {
        if (entity === 'blueprint') {
            const bp = _stagingData.blueprints.find(b => b.id === id);
            if (bp) bp[field] = value;
        } else if (entity === 'item') {
            const it = _stagingData.items.find(i => i.item_id === id);
            if (it) it[field] = value;
        }
    }
    
    _render();
}

function _cancelEdit(input) {
    _render();
}

function _updatePhotoProposal(itemId, newProposal) {
    if (!Array.isArray(_stagingData.photo_proposals)) {
        _stagingData.photo_proposals = [];
    }
    const idx = _stagingData.photo_proposals.findIndex(p => p.item_id === itemId);
    if (idx !== -1) {
        _stagingData.photo_proposals[idx] = newProposal;
    } else {
        _stagingData.photo_proposals.push(newProposal);
    }
}

async function _handleRetryPhoto(e) {
    const btn    = e.currentTarget;
    const itemId = btn.dataset.itemId;
    const jobId  = btn.dataset.jobId;
    const feedback = prompt('Descrivi la foto che stai cercando (opzionale):');
    btn.disabled = true;
    btn.textContent = '⏳';
    try {
        const result = await api.retryPhoto(jobId, itemId, feedback || null);
        addMessage('✅ Ricerca foto aggiornata.', 'success');
        if (result?.data) {
            _updatePhotoProposal(itemId, result.data);
            _render();
        }
    } catch (err) {
        addMessage(`❌ Retry foto fallito: ${err.message}`, 'error');
        btn.disabled = false;
        btn.textContent = '🔄 Riprova';
    }
}

async function _handleManualPhoto(e) {
    const btn    = e.currentTarget;
    const itemId = btn.dataset.itemId;
    const jobId  = btn.dataset.jobId;
    const url = prompt('Inserisci URL immagine:');
    if (!url) return;
    
    btn.disabled = true;
    btn.textContent = '⏳';
    try {
        const result = await api.retryPhoto(jobId, itemId, null, url);
        addMessage('✅ Immagine aggiornata con successo.', 'success');
        if (result?.data) {
            _updatePhotoProposal(itemId, result.data);
            _render();
        }
    } catch (err) {
        addMessage(`❌ Aggiornamento fallito: ${err.message}`, 'error');
        btn.disabled = false;
        btn.textContent = '🔗 Inserisci URL';
    }
}

// ---- Operations queue ----

function _normalizeValue(field, rawStr) {
    if (LIST_FIELDS.has(field)) {
        return rawStr.split(',').map(s => s.trim().toLowerCase()).filter(Boolean);
    }
    if (field === 'quantity') return parseInt(rawStr, 10) || 1;
    return rawStr;
}

function _enqueueUpdate(entity, id, field, value) {
    if (entity === 'item') {
        const existingIdx = _operationsQueue.findIndex(
            op => op.op === 'update_item' && op.item_id === id
        );
        if (existingIdx !== -1) {
            _operationsQueue[existingIdx].fields = {
                ..._operationsQueue[existingIdx].fields,
                [field]: value,
            };
        } else {
            _operationsQueue.push({ op: 'update_item', item_id: id, fields: { [field]: value } });
        }
    } else if (entity === 'blueprint') {
        const existingIdx = _operationsQueue.findIndex(
            op => op.op === 'update_blueprint' && op.blueprint_id === id
        );
        if (existingIdx !== -1) {
            _operationsQueue[existingIdx].fields = {
                ..._operationsQueue[existingIdx].fields,
                [field]: value,
            };
        } else {
            _operationsQueue.push({ op: 'update_blueprint', blueprint_id: id, fields: { [field]: value } });
        }
    }
}

async function _handleRefresh() {
    try {
        const result = await api.pollJobStatus(_jobId);
        if (result && result.data) {
            _stagingData = result.data;
            _operationsQueue = [];
            _render();
            addMessage('🔄 Dati aggiornati dal server.', 'system');
        }
    } catch (err) {
        addMessage(`❌ Errore durante il refresh: ${err.message}`, 'error');
    }
}

async function _handleConfirm() {
    const btn = document.querySelector('.btn-confirm-import');
    if (btn) btn.disabled = true;
    try {
        await api.confirmIngestion(_jobId);
        addMessage('✅ Dati importati con successo nel catalogo.', 'success');
        document.dispatchEvent(new CustomEvent('ingestionDone'));
    } catch (err) {
        addMessage(`❌ Conferma fallita: ${err.message}`, 'error');
        if (btn) btn.disabled = false;
    }
}
