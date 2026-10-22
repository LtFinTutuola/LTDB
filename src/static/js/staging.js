/**
 * staging.js
 * Manages the staging split-screen panel:
 *  - Renders blueprint cards with click-to-edit fields
 *  - Maintains an operations queue with deduplication
 *  - Submits revision batches to PUT /ingestion/staging/{job_id}
 *  - Handles photo proposal display and retry
 */
import * as api from './api.js?v=20261022_6';
import {
    appState, Mode, setMode,
    addMessage, getDetailPanel, exitSplitScreen, _esc,
} from './main.js?v=20261022_6';
import { setupBarcodeScanner } from './barcode_scanner.js?v=20261022_6';
import { requestPhotoUpload } from './photo_upload_helper.js?v=20261022_6';

// ---- Module state ----
let _activePhotoIndices = {};
let _jobId = null;
let _stagingData = null;
let _operationsQueue = [];
let _categoryHierarchy = null;
let _currentMobileBp = null;
let _currentMobileItem = null;

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
    } catch (err) {
        addMessage(`❌ Errore: ${err.message}`, 'error');
    }
}

// ---- Rendering ----

function _render() {
    const panel = getDetailPanel();
    if (!panel) return;

    const oldContent = document.getElementById('staging-content');
    const scrollPos = oldContent ? oldContent.scrollTop : 0;
    const wasMobilePanelOpen = panel.classList.contains('mobile-staging-panel-open');
    const activeItemId = _currentMobileItem?.item_id;
    const activeBpId = _currentMobileBp?.id;

    const grouped = _groupByBlueprint(_stagingData);
    const photoProposals = _stagingData?.photo_proposals || {};
    const itemsCount = (_stagingData?.items || []).length;

    let html = `
        <div class="detail-panel__header">
            <button class="btn-close-panel btn-close-panel--mobile" title="Torna">
                <span class="material-symbols-rounded">arrow_back</span>
            </button>
            <div class="detail-panel__header-title-box">
                <h2 class="detail-panel__title">Revisione dati</h2>
                <span class="detail-panel__count" style="font-size:12px;color:var(--text-muted)">${itemsCount} elementi</span>
            </div>
            <div class="detail-panel__header-actions">
                <button class="btn-refresh-panel" title="Ricarica">
                    <span class="material-symbols-rounded" style="font-size:20px;">refresh</span>
                </button>
                <button class="btn-close-panel btn-close-panel--desktop" title="Chiudi">✕</button>
            </div>
        </div>
        <div class="detail-panel__content" id="staging-content" style="flex:1;overflow-y:auto;">
    `;

    if (grouped.size === 0) {
        html += `<div class="empty-state"><span class="empty-state__icon">📋</span><p class="empty-state__text">Nessun elemento da rivedere</p></div>`;
    } else {
        // Desktop View: Blueprint Cards with Tables
        html += `<div class="staging-desktop-view">`;
        for (const [bpId, { blueprint, items }] of grouped) {
            html += _buildBlueprintCard(blueprint, items, photoProposals);
        }
        html += `</div>`;

        // Mobile View: Flat continuous list of items
        html += `<div class="staging-mobile-view">`;
        html += _buildMobileStagingList(grouped, photoProposals);
        html += `</div>`;
    }

    html += '</div>';

    const hasChanges = _operationsQueue.length > 0;
    const btnText = hasChanges ? 'Applica modifiche' : '✅ Conferma e importa';
    const btnClass = hasChanges ? 'btn-apply-changes' : 'btn-confirm-import';

    html += `
        <div class="detail-panel__footer" style="padding: var(--space-md) var(--space-lg); display: flex; justify-content: flex-end;">
            <button class="${btnClass} primary-btn" style="padding: 10px 20px; border-radius: var(--radius-pill); border: none; font-weight: 600; cursor: pointer; background: ${hasChanges ? 'var(--warning)' : 'var(--success)'}; color: #fff;">${btnText}</button>
        </div>
    `;

    // Dedicated Mobile Panel for Editing/Deleting Staged Article
    html += _buildMobileStagingPanel();

    panel.innerHTML = html;

    const newContent = document.getElementById('staging-content');
    if (newContent) {
        newContent.scrollTop = scrollPos;
    }

    panel.querySelectorAll('.btn-close-panel').forEach(btn => {
        btn.addEventListener('click', exitSplitScreen);
    });

    panel.querySelectorAll('.btn-refresh-panel').forEach(btn => {
        btn.addEventListener('click', _handleRefresh);
    });

    const applyBtn = panel.querySelector('.btn-apply-changes');
    if (applyBtn) applyBtn.addEventListener('click', submitRevisions);

    const confirmBtn = panel.querySelector('.btn-confirm-import');
    if (confirmBtn) confirmBtn.addEventListener('click', _handleConfirm);

    _attachListeners();

    // Preserve mobile edit panel state if open during refresh or photo retry
    if (wasMobilePanelOpen && activeItemId && activeBpId) {
        const refreshedBp = _stagingData?.blueprints?.find(b => b.id === activeBpId);
        const refreshedIt = _stagingData?.items?.find(i => i.item_id === activeItemId);
        if (refreshedBp && refreshedIt) {
            openStagingMobilePanel(refreshedBp, refreshedIt);
        } else {
            panel.classList.remove('mobile-staging-panel-open');
        }
    } else {
        panel.classList.remove('mobile-staging-panel-open');
    }
}

function _buildMobileStagingList(grouped, photoProposals) {
    let listHtml = '<div class="staging-mobile-list">';
    const brandName = _stagingData?.brand_name || 'Brand';

    for (const [bpId, { blueprint, items }] of grouped) {
        const catDesc = blueprint.category?.description || '';
        const subCatDesc = blueprint.sub_category?.description || '';
        let catText = catDesc;
        if (subCatDesc) {
            catText = catText ? `${catText} ↳ ${subCatDesc}` : subCatDesc;
        }

        const isNewBadge = blueprint.is_new ? '<span class="badge-new" style="margin-left: 6px; font-size: 10px; padding: 1px 6px;">Nuovo</span>' : '';

        for (const item of items) {
            const proposal = _findProposal(photoProposals, item.item_id);
            const photoUrl = proposal?.url || proposal?.photo_url || null;

            const photoHtml = photoUrl
                ? `<img src="${_esc(photoUrl)}" alt="" onerror="this.style.display='none';if(this.nextElementSibling)this.nextElementSibling.style.display='block';"><span class="photo-placeholder" style="display:none;font-size:24px;">🖼</span>`
                : `<span class="photo-placeholder" style="font-size:24px;">🖼</span>`;

            let codeInfo = '';
            if (item.vendor_code && item.barcode) {
                codeInfo = `Cod: ${_esc(item.vendor_code)} • EAN: ${_esc(item.barcode)}`;
            } else if (item.vendor_code) {
                codeInfo = `Cod: ${_esc(item.vendor_code)}`;
            } else if (item.barcode) {
                codeInfo = `EAN: ${_esc(item.barcode)}`;
            }

            const colorsStr = (item.colors || []).join(', ');
            let metaBottomText = `Qtà: ${item.quantity ?? 1}`;
            if (codeInfo) {
                metaBottomText = `${codeInfo} • ${metaBottomText}`;
            }
            if (colorsStr) {
                metaBottomText = `${metaBottomText} • ${_esc(colorsStr)}`;
            }

            listHtml += `
                <div class="staging-mobile-item" data-item-id="${_esc(item.item_id)}" data-bp-id="${_esc(blueprint.id)}">
                    <div class="warehouse-row__cell--photo">
                        <div class="warehouse-row__photo">
                            ${photoHtml}
                        </div>
                    </div>
                    <div class="warehouse-row__cell--info">
                        <div class="warehouse-row__mobile-meta-top">
                            <span class="warehouse-row__mobile-brand">${_esc(brandName)}</span>
                            ${catText ? `<span class="warehouse-row__mobile-cat-divider">•</span><span class="warehouse-row__mobile-category">${_esc(catText)}</span>` : ''}
                        </div>
                        <div class="warehouse-row__title">${_esc(blueprint.article_name || 'Articolo')}${isNewBadge}</div>
                        <div class="warehouse-row__mobile-meta-bottom">
                            <span class="stock-badge stock-badge--available">${metaBottomText}</span>
                        </div>
                    </div>
                </div>
            `;
        }
    }

    listHtml += '</div>';
    return listHtml;
}

function _buildMobileStagingPanel() {
    return `
        <div id="staging-mobile-panel" class="article-mobile-panel staging-mobile-panel" aria-label="Dettaglio e Modifica Articolo Staging">
            <div class="article-mobile-panel__topbar">
                <button type="button" id="btn-staging-mobile-back" class="article-panel-btn-back" title="Torna alla lista">
                    <span class="material-symbols-rounded">arrow_back</span>
                </button>
                <h3 id="staging-mobile-title" class="article-panel-title">Dettaglio Staging</h3>
                <button type="button" id="btn-staging-mobile-delete" class="article-panel-btn-delete" title="Rimuovi Articolo">
                    <span class="material-symbols-rounded">delete</span>
                </button>
            </div>
            <div class="article-mobile-panel__body">
                <form id="form-staging-mobile" class="mobile-panel-form">
                    <!-- Photo and Meta Preview Card -->
                    <div class="mobile-art-preview" style="display: flex; gap: 14px; align-items: center; background: var(--bg-app); padding: 12px; border-radius: var(--radius-sm); border: 1px solid var(--border-color); margin-bottom: 14px;">
                        <div id="mob-staging-photo-wrap" style="width: 64px; height: 64px; border-radius: var(--radius-xs); overflow: hidden; background: var(--bg-elevated); display: flex; align-items: center; justify-content: center; flex-shrink: 0; cursor: zoom-in;">
                            <img id="mob-staging-photo" src="" alt="" style="width: 100%; height: 100%; object-fit: cover;">
                            <span id="mob-staging-photo-placeholder" style="font-size: 28px; display: none;">🖼</span>
                        </div>
                        <div style="flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 4px;">
                            <span id="mob-staging-brand" style="color: var(--accent); font-weight: 600; font-size: 13px; text-transform: uppercase;"></span>
                            <span id="mob-staging-item-meta" style="color: var(--text-secondary); font-size: 12px;"></span>
                            <div style="display: flex; gap: 6px; margin-top: 4px;">
                                <button type="button" id="mob-staging-btn-search" class="btn-photo-action" title="Cerca su Google">🔍 Cerca</button>
                                <button type="button" id="mob-staging-btn-upload" class="btn-photo-action" title="Carica Foto">📁 Carica</button>
                                <button type="button" id="mob-staging-btn-manual" class="btn-photo-action" title="Inserisci URL">🔗 URL</button>
                                <button type="button" id="mob-staging-btn-retry" class="btn-photo-action" title="Riprova con AI">🔄 Riprova</button>
                            </div>
                        </div>
                    </div>

                    <div class="form-group" style="display: flex; flex-direction: column; gap: 6px; margin-bottom: 12px;">
                        <label for="mob-staging-name" style="font-size: 13px; font-weight: 500; color: var(--text-secondary);">Nome Articolo *</label>
                        <input type="text" id="mob-staging-name" class="form-control" required>
                    </div>

                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 12px;">
                        <div class="form-group" style="display: flex; flex-direction: column; gap: 6px;">
                            <label for="mob-staging-macro-cat" style="font-size: 13px; font-weight: 500; color: var(--text-secondary);">Macro Categoria</label>
                            <select id="mob-staging-macro-cat" class="form-control category-select">
                                <option value="">Macro Categoria...</option>
                            </select>
                        </div>
                        <div class="form-group" style="display: flex; flex-direction: column; gap: 6px;">
                            <label for="mob-staging-sub-cat" style="font-size: 13px; font-weight: 500; color: var(--text-secondary);">Sotto Categoria</label>
                            <select id="mob-staging-sub-cat" class="form-control category-select">
                                <option value="">Sotto Categoria...</option>
                            </select>
                        </div>
                    </div>

                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 12px;">
                        <div class="form-group" style="display: flex; flex-direction: column; gap: 6px;">
                            <label for="mob-staging-vendor-code" style="font-size: 13px; font-weight: 500; color: var(--text-secondary);">Codice Fornitore</label>
                            <input type="text" id="mob-staging-vendor-code" class="form-control" placeholder="Es. HWVG87...">
                        </div>
                        <div class="form-group" style="display: flex; flex-direction: column; gap: 6px;">
                            <label for="mob-staging-barcode" style="font-size: 13px; font-weight: 500; color: var(--text-secondary);">Barcode (EAN)</label>
                            <div style="display: flex; gap: 6px;">
                                <input type="text" id="mob-staging-barcode" class="form-control" placeholder="Es. 800000...">
                                <button type="button" id="btn-scan-mob-staging-barcode" class="btn-scan-field" title="Scansiona barcode con la fotocamera">
                                    <span class="material-symbols-rounded" style="font-size: 18px;">photo_camera</span>
                                </button>
                            </div>
                        </div>
                    </div>

                    <div style="display: grid; grid-template-columns: 1fr 2fr; gap: 10px; margin-bottom: 12px;">
                        <div class="form-group" style="display: flex; flex-direction: column; gap: 6px;">
                            <label for="mob-staging-qty" style="font-size: 13px; font-weight: 500; color: var(--text-secondary);">Quantità</label>
                            <input type="number" id="mob-staging-qty" class="form-control" min="1" value="1">
                        </div>
                        <div class="form-group" style="display: flex; flex-direction: column; gap: 6px;">
                            <label for="mob-staging-colors" style="font-size: 13px; font-weight: 500; color: var(--text-secondary);">Colori (separati da virgola)</label>
                            <input type="text" id="mob-staging-colors" class="form-control" placeholder="Es. Nero, Cognac">
                        </div>
                    </div>

                    <div class="form-group" style="display: flex; flex-direction: column; gap: 6px; margin-bottom: 12px;">
                        <label for="mob-staging-desc" style="font-size: 13px; font-weight: 500; color: var(--text-secondary);">Descrizione Breve</label>
                        <textarea id="mob-staging-desc" class="form-control" rows="2" placeholder="Descrizione breve..."></textarea>
                    </div>

                    <div class="form-group" style="display: flex; flex-direction: column; gap: 6px; margin-bottom: 12px;">
                        <label for="mob-staging-ext-desc" style="font-size: 13px; font-weight: 500; color: var(--text-secondary);">Descrizione Estesa</label>
                        <textarea id="mob-staging-ext-desc" class="form-control" rows="3" placeholder="Descrizione estesa..."></textarea>
                    </div>

                    <div class="form-group" style="display: flex; flex-direction: column; gap: 6px; margin-bottom: 12px;">
                        <label for="mob-staging-tags" style="font-size: 13px; font-weight: 500; color: var(--text-secondary);">Tags (separati da virgola)</label>
                        <input type="text" id="mob-staging-tags" class="form-control" placeholder="Es. borsa, tracolla">
                    </div>

                    <div class="form-group" style="display: flex; flex-direction: column; gap: 6px; margin-bottom: 12px;">
                        <label for="mob-staging-materials" style="font-size: 13px; font-weight: 500; color: var(--text-secondary);">Materiali (separati da virgola)</label>
                        <input type="text" id="mob-staging-materials" class="form-control" placeholder="Es. ecopelle, metallo">
                    </div>

                    <button type="submit" id="btn-save-staging-mobile" class="btn-primary" style="margin-top: 14px; margin-bottom: 24px; padding: 12px; width: 100%; font-weight: 600;">
                        Salva Modifiche
                    </button>
                </form>
            </div>
        </div>
    `;
}

function _groupByBlueprint(data) {
    const map = new Map();
    const bpMap = {};
    for (const bp of (data?.blueprints || [])) bpMap[bp.id] = bp;
    for (const item of (data?.items || [])) {
        const bpId = item.article_blueprint_id;
        if (!map.has(bpId)) map.set(bpId, { blueprint: bpMap[bpId] || { id: bpId, article_name: 'Blueprint ' + bpId.slice(0, 8), description: '', tags: [], materials: [] }, items: [] });
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
            <td style="padding: 4px 8px; width: 85px;"><input type="number" min="1" class="staging-qty-input" data-item-id="${_esc(item.item_id)}" value="${_esc(String(item.quantity ?? 1))}" style="width: 65px; padding: 4px 6px; text-align: center; border-radius: var(--radius-xs); border: 1px solid var(--border-color); background: var(--bg-app); color: var(--text-primary); font-size: 13px; font-weight: 600;"></td>
            <td><span class="editable" data-entity="item" data-id="${_esc(item.item_id)}" data-field="colors">${_esc(colors)}</span></td>
            <td><span class="editable" data-entity="blueprint" data-id="${_esc(bp.id)}" data-field="materials">${_esc(materials)}</span></td>
        </tr>
        `;
    }).join('');

    return `
    <table class="items-table">
        <thead><tr>
            <th>Codice</th><th>Barcode</th><th style="width: 85px;">Qtà</th><th>Colori</th><th>Materiali</th>
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

function _getPhotoSearchQuery(bpId, item, proposal) {
    if (proposal && proposal.search_query) {
        return proposal.search_query;
    }
    if (proposal && Array.isArray(proposal._photo_search_ctx) && proposal._photo_search_ctx.length > 0) {
        for (let i = proposal._photo_search_ctx.length - 1; i >= 0; i--) {
            const msg = proposal._photo_search_ctx[i];
            if (msg.role === 'model' && msg.content) {
                return msg.content.trim();
            }
        }
    }
    // Fallback: construct query from staging data
    const bp = _stagingData?.blueprints?.find(b => b.id === bpId);
    const brand = _stagingData?.brand_name || '';
    const article = bp?.article_name || '';
    const vendorCode = item?.vendor_code || '';
    const colors = (item?.colors || []).join(' ');
    const parts = [brand, article, vendorCode, colors, 'packshot'].filter(Boolean);
    return parts.join(' ').trim();
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
    const searchQuery = _getPhotoSearchQuery(bpId, currentItem, proposal);

    const imgHtml = url
        ? `<img src="${_esc(url)}" alt="foto" onerror="this.style.display='none'" onclick="window._openImageModal(this.src)">`
        : `<div class="photo-placeholder" style="display:flex;align-items:center;justify-content:center;color:var(--text-muted);font-size:32px;">🖼</div>`;

    const colorsStr = (currentItem.colors || []).join(', ');
    const colorLabel = colorsStr || 'Nessun colore';

    let navHtml = '';
    if (items.length > 1) {
        navHtml = `
            <div class="photo-carousel-nav" style="display:flex;justify-content:space-between;align-items:center;margin-top:8px;">
                <button class="btn-prev-photo" data-bp-id="${_esc(bpId)}" style="background:var(--bg-elevated);border:1px solid var(--border-color);border-radius:4px;cursor:pointer;padding:2px 8px;color:var(--text-primary);">◀</button>
                <span style="font-size:12px;font-weight:600;color:var(--text-secondary);text-align:center;flex:1;">${_esc(colorLabel)}<br><span style="font-weight:normal;">(${currentIndex + 1}/${items.length})</span></span>
                <button class="btn-next-photo" data-bp-id="${_esc(bpId)}" style="background:var(--bg-elevated);border:1px solid var(--border-color);border-radius:4px;cursor:pointer;padding:2px 8px;color:var(--text-primary);">▶</button>
            </div>
        `;
    }

    return `
    <div class="blueprint-card__photo-col" style="display:flex;flex-direction:column;">
        ${imgHtml}
        ${navHtml}
        <button class="btn-search-photo" data-item-id="${_esc(currentItem.item_id)}" data-search-query="${_esc(searchQuery)}" style="margin-top:auto;" title="Cerca su Google">🔍 Cerca</button>
        <button class="btn-upload-photo" data-item-id="${_esc(currentItem.item_id)}" data-job-id="${_esc(_jobId)}" style="margin-top:4px;" title="Carica foto dal dispositivo">📁 Carica Foto</button>
        <button class="btn-manual-photo" data-item-id="${_esc(currentItem.item_id)}" data-job-id="${_esc(_jobId)}" style="margin-top:4px;">🔗 Inserisci URL</button>
        <button class="btn-retry-photo" data-item-id="${_esc(currentItem.item_id)}" data-job-id="${_esc(_jobId)}" style="margin-top:4px;">🔄 Riprova</button>
    </div>`;
}

// Global modal function
window._openImageModal = function (url) {
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

    // Staging quantity input
    content.querySelectorAll('.staging-qty-input').forEach(input => {
        input.addEventListener('change', (e) => {
            const itemId = e.target.dataset.itemId;
            const newQty = parseInt(e.target.value, 10) || 1;
            e.target.value = newQty;
            const it = _stagingData.items.find(i => i.item_id === itemId);
            if (it && it.quantity !== newQty) {
                it.quantity = newQty;
                _enqueueUpdate('item', itemId, 'quantity', newQty);
                _render();
            }
        });
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

    // Photo retry, search, manual url, and upload
    content.querySelectorAll('.btn-upload-photo').forEach(btn => {
        btn.addEventListener('click', _handleUploadPhoto);
    });
    content.querySelectorAll('.btn-retry-photo').forEach(btn => {
        btn.addEventListener('click', _handleRetryPhoto);
    });
    content.querySelectorAll('.btn-search-photo').forEach(btn => {
        btn.addEventListener('click', _handleSearchPhoto);
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

    _attachMobileListeners(getDetailPanel());
}

function _attachMobileListeners(panel) {
    if (!panel) return;

    // Mobile staging list rows click -> open mobile panel
    panel.querySelectorAll('.staging-mobile-item').forEach(row => {
        row.addEventListener('click', () => {
            const itemId = row.dataset.itemId;
            const bpId = row.dataset.bpId;
            const bp = _stagingData?.blueprints?.find(b => b.id === bpId);
            const item = _stagingData?.items?.find(i => i.item_id === itemId);
            if (bp && item) {
                openStagingMobilePanel(bp, item);
            }
        });
    });

    // Mobile panel back button
    const backBtn = panel.querySelector('#btn-staging-mobile-back');
    if (backBtn) backBtn.addEventListener('click', closeStagingMobilePanel);

    // Mobile panel delete button
    const deleteBtn = panel.querySelector('#btn-staging-mobile-delete');
    if (deleteBtn) deleteBtn.addEventListener('click', _handleMobileDelete);

    // Mobile panel form submit
    const form = panel.querySelector('#form-staging-mobile');
    if (form) form.addEventListener('submit', _handleMobileSave);

    // Mobile panel photo actions
    const btnUpload = panel.querySelector('#mob-staging-btn-upload');
    if (btnUpload) btnUpload.addEventListener('click', _handleUploadPhoto);

    const btnSearch = panel.querySelector('#mob-staging-btn-search');
    if (btnSearch) btnSearch.addEventListener('click', _handleSearchPhoto);

    const btnManual = panel.querySelector('#mob-staging-btn-manual');
    if (btnManual) btnManual.addEventListener('click', _handleManualPhoto);

    const btnRetry = panel.querySelector('#mob-staging-btn-retry');
    if (btnRetry) btnRetry.addEventListener('click', _handleRetryPhoto);

    // Mobile panel barcode scanner
    const btnScanBarcode = panel.querySelector('#btn-scan-mob-staging-barcode');
    if (btnScanBarcode) {
        setupBarcodeScanner({
            cameraInputId: 'mob-staging-barcode-camera-input',
            triggerBtnIds: ['btn-scan-mob-staging-barcode'],
            onSuccess: (barcode) => {
                const barcodeInput = panel.querySelector('#mob-staging-barcode');
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
}

export function openStagingMobilePanel(bp, item) {
    _currentMobileBp = bp;
    _currentMobileItem = item;
    const panel = getDetailPanel();
    const mobilePanel = document.getElementById('staging-mobile-panel');
    if (!panel || !mobilePanel) return;

    // 1. Photo and preview
    const photoProposals = _stagingData?.photo_proposals || {};
    const proposal = _findProposal(photoProposals, item.item_id);
    const photoUrl = proposal?.url || proposal?.photo_url || null;
    const searchQuery = _getPhotoSearchQuery(bp.id, item, proposal);

    const photoImg = document.getElementById('mob-staging-photo');
    const photoPlaceholder = document.getElementById('mob-staging-photo-placeholder');
    const photoWrap = document.getElementById('mob-staging-photo-wrap');

    if (photoImg && photoPlaceholder) {
        if (photoUrl) {
            photoImg.src = photoUrl;
            photoImg.style.display = 'block';
            photoPlaceholder.style.display = 'none';
            if (photoWrap) {
                photoWrap.onclick = () => window._openImageModal(photoUrl);
            }
        } else {
            photoImg.src = '';
            photoImg.style.display = 'none';
            photoPlaceholder.style.display = 'block';
            if (photoWrap) photoWrap.onclick = null;
        }
    }

    const brandEl = document.getElementById('mob-staging-brand');
    if (brandEl) brandEl.textContent = _stagingData?.brand_name || 'Brand';

    const itemMetaEl = document.getElementById('mob-staging-item-meta');
    if (itemMetaEl) {
        const parts = [];
        if (item.vendor_code) parts.push(`Cod: ${item.vendor_code}`);
        if (item.barcode) parts.push(`EAN: ${item.barcode}`);
        parts.push(`Qtà: ${item.quantity ?? 1}`);
        itemMetaEl.textContent = parts.join(' • ');
    }

    const btnUpload = document.getElementById('mob-staging-btn-upload');
    if (btnUpload) {
        btnUpload.dataset.itemId = item.item_id;
        btnUpload.dataset.jobId = _jobId;
    }
    const btnSearch = document.getElementById('mob-staging-btn-search');
    if (btnSearch) {
        btnSearch.dataset.searchQuery = searchQuery;
    }
    const btnManual = document.getElementById('mob-staging-btn-manual');
    if (btnManual) {
        btnManual.dataset.itemId = item.item_id;
        btnManual.dataset.jobId = _jobId;
    }
    const btnRetry = document.getElementById('mob-staging-btn-retry');
    if (btnRetry) {
        btnRetry.dataset.itemId = item.item_id;
        btnRetry.dataset.jobId = _jobId;
    }

    // 2. Categories
    const macroCatSelect = document.getElementById('mob-staging-macro-cat');
    const subCatSelect = document.getElementById('mob-staging-sub-cat');
    const currentMacro = bp.category?.description || '';
    const currentSub = bp.sub_category?.description || '';

    const hasHierarchy = _categoryHierarchy && Object.keys(_categoryHierarchy).length > 0;
    if (macroCatSelect) {
        let macroHtml = '<option value="">Macro Categoria...</option>';
        if (hasHierarchy) {
            macroHtml += Object.keys(_categoryHierarchy).map(c =>
                `<option value="${_esc(c)}" ${c === currentMacro ? 'selected' : ''}>${_esc(c)}</option>`
            ).join('');
        } else if (currentMacro) {
            macroHtml += `<option value="${_esc(currentMacro)}" selected>${_esc(currentMacro)}</option>`;
        }
        macroCatSelect.innerHTML = macroHtml;
    }

    function _updateSubCatDropdown(selectedMacro, selectedSub) {
        if (!subCatSelect) return;
        let subHtml = '<option value="">Sotto Categoria...</option>';
        if (hasHierarchy && selectedMacro && _categoryHierarchy[selectedMacro]) {
            const subs = Object.keys(_categoryHierarchy[selectedMacro].sub_categories || {});
            subHtml += subs.map(sc =>
                `<option value="${_esc(sc)}" ${sc === selectedSub ? 'selected' : ''}>${_esc(sc)}</option>`
            ).join('');
        } else if (selectedSub) {
            subHtml += `<option value="${_esc(selectedSub)}" selected>${_esc(selectedSub)}</option>`;
        }
        subCatSelect.innerHTML = subHtml;
    }

    _updateSubCatDropdown(currentMacro, currentSub);

    if (macroCatSelect) {
        macroCatSelect.onchange = () => {
            _updateSubCatDropdown(macroCatSelect.value, '');
        };
    }

    // 3. Inputs
    const nameInput = document.getElementById('mob-staging-name');
    if (nameInput) nameInput.value = bp.article_name || '';

    const vendorInput = document.getElementById('mob-staging-vendor-code');
    if (vendorInput) vendorInput.value = item.vendor_code || '';

    const barcodeInput = document.getElementById('mob-staging-barcode');
    if (barcodeInput) barcodeInput.value = item.barcode || '';

    const qtyInput = document.getElementById('mob-staging-qty');
    if (qtyInput) qtyInput.value = item.quantity ?? 1;

    const colorsInput = document.getElementById('mob-staging-colors');
    if (colorsInput) colorsInput.value = (item.colors || []).join(', ');

    const descInput = document.getElementById('mob-staging-desc');
    if (descInput) descInput.value = bp.description || '';

    const extDescInput = document.getElementById('mob-staging-ext-desc');
    if (extDescInput) extDescInput.value = bp.extended_description || '';

    const tagsInput = document.getElementById('mob-staging-tags');
    if (tagsInput) tagsInput.value = (bp.tags || []).join(', ');

    const materialsInput = document.getElementById('mob-staging-materials');
    if (materialsInput) materialsInput.value = (bp.materials || item.materials || []).join(', ');

    // 4. Slide in panel
    panel.classList.add('mobile-staging-panel-open');
}

export function closeStagingMobilePanel() {
    const panel = getDetailPanel();
    if (panel) panel.classList.remove('mobile-staging-panel-open');
    _currentMobileBp = null;
    _currentMobileItem = null;
}

function _handleMobileDelete(e) {
    if (e) e.preventDefault();
    if (!_currentMobileItem || !_currentMobileBp) return;

    if (!confirm('Rimuovere questo articolo dallo staging?')) return;

    const itemId = _currentMobileItem.item_id;
    const bpId = _currentMobileBp.id;

    _enqueueDelete(itemId);

    // Remove item from local staging data
    _stagingData.items = (_stagingData.items || []).filter(i => i.item_id !== itemId);
    const hasRemaining = (_stagingData.items || []).some(i => i.article_blueprint_id === bpId);
    if (!hasRemaining) {
        _stagingData.blueprints = (_stagingData.blueprints || []).filter(b => b.id !== bpId);
    }

    closeStagingMobilePanel();
    _render();
    addMessage('Articolo rimosso dallo staging.', 'system');
}

function _handleMobileSave(e) {
    if (e) e.preventDefault();
    if (!_currentMobileItem || !_currentMobileBp) return;

    const bp = _currentMobileBp;
    const item = _currentMobileItem;

    const nameInput = document.getElementById('mob-staging-name');
    const macroCatSelect = document.getElementById('mob-staging-macro-cat');
    const subCatSelect = document.getElementById('mob-staging-sub-cat');
    const vendorInput = document.getElementById('mob-staging-vendor-code');
    const barcodeInput = document.getElementById('mob-staging-barcode');
    const qtyInput = document.getElementById('mob-staging-qty');
    const colorsInput = document.getElementById('mob-staging-colors');
    const descInput = document.getElementById('mob-staging-desc');
    const extDescInput = document.getElementById('mob-staging-ext-desc');
    const tagsInput = document.getElementById('mob-staging-tags');
    const materialsInput = document.getElementById('mob-staging-materials');

    const newName = nameInput ? nameInput.value.trim() : '';
    const newMacroCat = macroCatSelect ? macroCatSelect.value.trim() : '';
    const newSubCat = subCatSelect ? subCatSelect.value.trim() : '';
    const newVendor = vendorInput ? vendorInput.value.trim() : '';
    const newBarcode = barcodeInput ? barcodeInput.value.trim() : '';
    const newQty = qtyInput ? (parseInt(qtyInput.value, 10) || 1) : 1;
    const newColors = colorsInput ? colorsInput.value.split(',').map(s => s.trim().toLowerCase()).filter(Boolean) : [];
    const newDesc = descInput ? descInput.value.trim() : '';
    const newExtDesc = extDescInput ? extDescInput.value.trim() : '';
    const newTags = tagsInput ? tagsInput.value.split(',').map(s => s.trim().toLowerCase()).filter(Boolean) : [];
    const newMaterials = materialsInput ? materialsInput.value.split(',').map(s => s.trim().toLowerCase()).filter(Boolean) : [];

    // Check Blueprint modifications
    if (newName && newName !== (bp.article_name || '')) {
        bp.article_name = newName;
        _enqueueUpdate('blueprint', bp.id, 'article_name', newName);
    }

    const curMacro = bp.category?.description || '';
    const curSub = bp.sub_category?.description || '';
    if (newMacroCat !== curMacro) {
        bp.category = newMacroCat ? { description: newMacroCat } : null;
        _enqueueUpdate('blueprint', bp.id, 'category', bp.category);
    }
    if (newSubCat !== curSub) {
        bp.sub_category = newSubCat ? { description: newSubCat } : null;
        _enqueueUpdate('blueprint', bp.id, 'sub_category', bp.sub_category);
    }

    if (newDesc !== (bp.description || '')) {
        bp.description = newDesc;
        _enqueueUpdate('blueprint', bp.id, 'description', newDesc);
    }

    if (newExtDesc !== (bp.extended_description || '')) {
        bp.extended_description = newExtDesc;
        _enqueueUpdate('blueprint', bp.id, 'extended_description', newExtDesc);
    }

    const curTagsStr = (bp.tags || []).join(',');
    const newTagsStr = newTags.join(',');
    if (newTagsStr !== curTagsStr) {
        bp.tags = newTags;
        _enqueueUpdate('blueprint', bp.id, 'tags', newTags);
    }

    const curMaterialsStr = (bp.materials || []).join(',');
    const newMaterialsStr = newMaterials.join(',');
    if (newMaterialsStr !== curMaterialsStr) {
        bp.materials = newMaterials;
        _enqueueUpdate('blueprint', bp.id, 'materials', newMaterials);
    }

    // Check Item modifications
    if (newVendor !== (item.vendor_code || '')) {
        item.vendor_code = newVendor;
        _enqueueUpdate('item', item.item_id, 'vendor_code', newVendor);
    }

    if (newBarcode !== (item.barcode || '')) {
        item.barcode = newBarcode;
        _enqueueUpdate('item', item.item_id, 'barcode', newBarcode);
    }

    if (newQty !== (item.quantity ?? 1)) {
        item.quantity = newQty;
        _enqueueUpdate('item', item.item_id, 'quantity', newQty);
    }

    const curColorsStr = (item.colors || []).join(',');
    const newColorsStr = newColors.join(',');
    if (newColorsStr !== curColorsStr) {
        item.colors = newColors;
        _enqueueUpdate('item', item.item_id, 'colors', newColors);
    }

    closeStagingMobilePanel();
    _render();
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
    const id = span.dataset.id;
    const field = span.dataset.field;

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
    input.dataset.id = id;
    input.dataset.field = field;
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

    input.addEventListener('blur', () => _commitEdit(input));
    input.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter') { ev.preventDefault(); input.blur(); }
        if (ev.key === 'Escape') { ev.preventDefault(); _cancelEdit(input); }
    });
}

function _commitEdit(input) {
    const entity = input.dataset.entity;
    const id = input.dataset.id;
    const field = input.dataset.field;
    const newRaw = input.value.trim();
    const origVal = input.dataset.originalVal;
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

async function _handleUploadPhoto(e) {
    const btn = e.currentTarget;
    const itemId = btn.dataset.itemId;
    const jobId = btn.dataset.jobId || _jobId;
    if (!itemId || !jobId) return;

    requestPhotoUpload({
        onSuccess: async (compressedBlob) => {
            addMessage('Caricamento foto in corso...', 'info');
            btn.disabled = true;
            try {
                const result = await api.uploadStagingPhoto(jobId, itemId, compressedBlob);
                if (result?.data) {
                    if (result.data.photo_id) {
                        api.invalidatePhotoCache(result.data.photo_id);
                    } else if (result.data.photo_url) {
                        api.invalidatePhotoCache(result.data.photo_url);
                    }
                    _updatePhotoProposal(itemId, result.data);
                    _render();
                    addMessage('✅ Foto caricata con successo!', 'success');
                    const photoImg = document.getElementById('mob-staging-photo');
                    const photoPlaceholder = document.getElementById('mob-staging-photo-placeholder');
                    if (photoImg && photoPlaceholder && result.data.photo_url) {
                        photoImg.src = result.data.photo_url;
                        photoImg.style.display = 'block';
                        photoPlaceholder.style.display = 'none';
                    }
                }
            } catch (err) {
                addMessage(`❌ Upload foto fallito: ${err.message}`, 'error');
            } finally {
                btn.disabled = false;
            }
        },
        onError: (err) => {
            addMessage(`❌ Errore selezione foto: ${err.message || err}`, 'error');
        }
    });
}

async function _handleRetryPhoto(e) {
    const btn = e.currentTarget;
    const itemId = btn.dataset.itemId;
    const jobId = btn.dataset.jobId;
    const feedback = prompt('Descrivi la foto che stai cercando (opzionale):');
    btn.disabled = true;
    btn.textContent = '⏳';
    try {
        const result = await api.retryPhoto(jobId, itemId, feedback || null);
        if (result?.data) {
            if (result.data.photo_id) {
                api.invalidatePhotoCache(result.data.photo_id);
            } else if (result.data.photo_url) {
                api.invalidatePhotoCache(result.data.photo_url);
            }
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
    const btn = e.currentTarget;
    const itemId = btn.dataset.itemId;
    const jobId = btn.dataset.jobId;
    const url = prompt('Inserisci URL immagine:');
    if (!url) return;

    btn.disabled = true;
    btn.textContent = '⏳';
    try {
        const result = await api.retryPhoto(jobId, itemId, null, url);
        if (result?.data) {
            if (result.data.photo_id) {
                api.invalidatePhotoCache(result.data.photo_id);
            } else if (result.data.photo_url) {
                api.invalidatePhotoCache(result.data.photo_url);
            }
            _updatePhotoProposal(itemId, result.data);
            _render();
        }
    } catch (err) {
        addMessage(`❌ Aggiornamento fallito: ${err.message}`, 'error');
        btn.disabled = false;
        btn.textContent = '🔗 Inserisci URL';
    }
}

function _handleSearchPhoto(e) {
    const btn = e.currentTarget;
    const query = btn.dataset.searchQuery;
    if (!query) {
        addMessage('⚠️ Nessuna query di ricerca disponibile.', 'warn');
        return;
    }
    const searchUrl = `https://www.google.com/search?tbm=isch&q=${encodeURIComponent(query)}`;
    window.open(searchUrl, '_blank', 'noopener,noreferrer');
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

function _enqueueDelete(itemId) {
    _operationsQueue = _operationsQueue.filter(
        op => !(op.op === 'update_item' && op.item_id === itemId)
    );
    _operationsQueue.push({ op: 'delete_item', item_id: itemId });
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
    const btn = document.querySelector('.btn-confirm-import') || document.querySelector('.btn-apply-changes');
    if (btn) btn.disabled = true;
    try {
        if (_operationsQueue.length > 0) {
            const revRes = await api.submitRevisions(_jobId, _operationsQueue);
            _operationsQueue = [];
            if (revRes?.data) _stagingData = revRes.data;
        }
        await api.confirmIngestion(_jobId);
        addMessage('✅ Dati importati con successo nel catalogo.', 'success');
        document.dispatchEvent(new CustomEvent('ingestionDone', { detail: { jobId: _jobId } }));
    } catch (err) {
        addMessage(`❌ Conferma fallita: ${err.message}`, 'error');
        if (btn) btn.disabled = false;
    }
}
