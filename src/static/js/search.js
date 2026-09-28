/**
 * search.js
 * Handles the semantic search flow:
 *  - Submits a natural language query to POST /catalog/search/semantic
 *  - Emits an Artifact Box in the chat flow (Punto 5)
 *  - Renders results as a list in the detail panel
 *  - In-place expansion of selected result (Punto 6): A, A, B, A, A pattern
 *  - Opens a popup for quick inline edits via PATCH /catalog/{blueprint_id}
 */
import * as api from './api.js';
import {
    Mode, setMode,
    addMessage, enterSplitScreen, exitSplitScreen,
    getDetailPanel, _esc,
} from './main.js';

// ---- Module state ----
let _lastResults = [];
let _lastQuery = '';
let _expandedIndex = null; // index of the currently expanded result card
let _currentArtifactBox = null;

// ---- Public API ----

export async function executeSearch(query) {
    setMode(Mode.SEARCH);
    _lastQuery = query;
    _expandedIndex = null;

    // Punto 5 refactored: create the artifact box immediately (like chat.js does for imports)
    const box = _emitSearchArtifactBox(query, -1, []);

    try {
        const result = await api.semanticSearch(query);
        const count = (result.results || []).length;
        _lastResults = result.results || [];

        // Update the existing artifact box with the results
        _updateSearchArtifactBox(box, query, count, _lastResults);

        // Auto-open the detail panel
        enterSplitScreen();
        _doRender();

        setMode(Mode.SEARCH);
    } catch (err) {
        _updateSearchArtifactBox(box, query, -2, [], err.message);
        addMessage(`❌ Ricerca fallita: ${err.message}`, 'error');
        setMode(Mode.IDLE);
    }
}

// ---- Artifact Box (Punto 5) ----

function _emitSearchArtifactBox(query, count, results) {
    const box = document.createElement('div');
    box.className = 'artifact-box'; // Initially not clickable while loading
    
    // Initial loading state
    box.innerHTML = `
        <span class="material-symbols-rounded artifact-box__icon">search</span>
        <div class="artifact-box__body">
            <span class="artifact-box__title">Ricerca &ldquo;${_esc(query)}&rdquo;</span>
            <span class="artifact-box__status" data-status-target><span class="spinner"></span>Ricerca in corso...</span>
        </div>
        <span class="material-symbols-rounded artifact-box__open-icon">open_in_full</span>
    `;

    // Usa addMessage() — lo stesso pattern di chat.js
    addMessage(box, 'system');
    return box;
}

function _updateSearchArtifactBox(box, query, count, results, errorMsg = null) {
    let subtitle = '';
    if (count === -2) {
        subtitle = `Errore: ${errorMsg}`;
    } else if (count === 0) {
        subtitle = 'Nessun risultato trovato';
    } else if (count === 1) {
        subtitle = 'Trovato 1 risultato corrispondente';
    } else {
        subtitle = `Trovati ${count} risultati corrispondenti`;
    }

    const statusEl = box.querySelector('[data-status-target]');
    if (statusEl) {
        statusEl.innerHTML = subtitle;
        if (count === -2) statusEl.style.color = 'var(--error)';
        else statusEl.style.color = 'var(--success)';
    }

    if (count >= 0) {
        box.classList.add('artifact-box--clickable');
        box.addEventListener('click', () => {
            _lastQuery = query;
            _lastResults = results;
            _expandedIndex = null;
            enterSplitScreen();
            _doRender();
        });
    }
}

// ---- Rendering (Punto 6: in-place expansion) ----

function _doRender() {
    const panel = getDetailPanel();

    if (!_lastResults || _lastResults.length === 0) {
        panel.innerHTML = `
            <div class="detail-panel__header">
                <div class="detail-panel__header-title-box">
                    <h2 class="detail-panel__title">Risultati ricerca</h2>
                    <span style="font-size:12px;color:var(--text-muted)">0 articoli</span>
                </div>
                <button class="btn-close-panel" title="Chiudi">✕</button>
            </div>
            <div class="detail-panel__content">
                <div class="empty-state">
                    <span class="empty-state__icon">🔍</span>
                    <p class="empty-state__text">Nessun risultato trovato</p>
                </div>
            </div>`;
        panel.querySelector('.btn-close-panel').addEventListener('click', _closePanel);
        return;
    }

    // Build the header
    const header = document.createElement('div');
    header.className = 'detail-panel__header';
    header.innerHTML = `
        <div class="detail-panel__header-title-box">
            <h2 class="detail-panel__title">Risultati ricerca</h2>
            <span style="font-size:12px;color:var(--text-muted)">${_lastResults.length} articoli</span>
        </div>
        <button class="btn-close-panel" title="Chiudi">✕</button>`;
    header.querySelector('.btn-close-panel').addEventListener('click', _closePanel);

    // Build content as a list, with in-place expansion (Punto 6)
    const content = document.createElement('div');
    content.className = 'detail-panel__content';

    const list = document.createElement('div');
    list.className = 'search-results-list';

    _lastResults.forEach((item, idx) => {
        if (_expandedIndex === idx) {
            // Template B: expanded card in-place
            const expanded = _buildExpandedCard(item, idx);
            list.appendChild(expanded);
        } else {
            // Template A: compact preview card
            const preview = _buildPreviewCard(item, idx);
            list.appendChild(preview);
        }
    });

    content.appendChild(list);

    // Replace panel content
    panel.innerHTML = '';
    panel.appendChild(header);
    panel.appendChild(content);
}

// ---- Template A: Compact Preview Card ----

function _buildPreviewCard(item, idx) {
    const wrapper = document.createElement('div');
    wrapper.className = 'search-preview-card';
    wrapper.dataset.idx = idx;

    const stockClass =
        item.stock > 5  ? 'available' :
        item.stock > 0  ? 'low' : 'zero';
    const stockLabel =
        item.stock > 0 ? `Giacenza: ${item.stock}` : 'Esaurito';

    const photoHtml = item.photo_id
        ? `<img src="${api.getPhotoUrl(item.photo_id)}" alt="${_esc(item.article_name)}"
               onerror="this.parentElement.innerHTML='<span class=\\"preview-card__photo-placeholder\\">🖼</span>'">`
        : `<span class="preview-card__photo-placeholder">🖼</span>`;

    const tags = (item.tags || []).slice(0, 2)
        .map(t => `<span class="chip chip--small">${_esc(t)}</span>`).join('');

    wrapper.innerHTML = `
        <div class="preview-card__photo">${photoHtml}</div>
        <div class="preview-card__info">
            <div class="preview-card__name" title="${_esc(item.article_name)}">${_esc(item.article_name)}</div>
            <span class="preview-card__brand">${_esc(item.brand_name)}</span>
            ${item.category_name ? `<span class="preview-card__cat">${_esc(item.category_name)}</span>` : ''}
            <div class="preview-card__tags">${tags}</div>
            <span class="stock-badge stock-badge--${stockClass}">${stockLabel}</span>
        </div>
        <span class="material-symbols-rounded preview-card__chevron">chevron_right</span>
    `;

    wrapper.addEventListener('click', (e) => {
        if (e.target.closest('.preview-card__edit-btn')) return;
        _expandedIndex = idx;
        _doRender();
        // Scroll expanded card into view
        setTimeout(() => {
            const panel = getDetailPanel();
            const content = panel.querySelector('.detail-panel__content');
            const expandedEl = panel.querySelector('.search-expanded-card');
            if (expandedEl && content) {
                expandedEl.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            }
        }, 50);
    });

    return wrapper;
}

// ---- Template B: Expanded Card in-place ----

function _buildExpandedCard(item, idx) {
    const wrapper = document.createElement('div');
    wrapper.className = 'search-expanded-card';
    wrapper.dataset.idx = idx;

    const photoHtml = item.photo_id
        ? `<img src="${api.getPhotoUrl(item.photo_id)}" alt="${_esc(item.article_name)}"
               onerror="this.style.display='none'" style="cursor:zoom-in;"
               onclick="if(window._openImageModal) window._openImageModal(this.src)">`
        : `<div class="expanded-card__photo-placeholder">🖼</div>`;

    const tagsHtml = (item.tags || []).map(t =>
        `<span class="chip chip--small">${_esc(t)}</span>`
    ).join('');

    const stockClass =
        item.stock > 5  ? 'available' :
        item.stock > 0  ? 'low' : 'zero';

    wrapper.innerHTML = `
        <div class="expanded-card__header">
            <button class="expanded-card__collapse-btn" title="Comprimi">
                <span class="material-symbols-rounded">expand_less</span>
            </button>
            <button class="expanded-card__edit-btn" title="Modifica">
                <span class="material-symbols-rounded">edit</span>
            </button>
        </div>
        <div class="expanded-card__body">
            <div class="expanded-card__photo">${photoHtml}</div>
            <div class="expanded-card__info">
                <div class="expanded-card__brand">${_esc(item.brand_name)}</div>
                <div class="expanded-card__name">${_esc(item.article_name)}</div>
                ${item.category_name ? `<div class="expanded-card__cat">${_esc(item.category_name)}</div>` : ''}
                ${item.description ? `<div class="expanded-card__desc">${_esc(item.description)}</div>` : ''}
                <div class="expanded-card__tags">${tagsHtml}</div>
                <div class="expanded-card__meta">
                    <span class="stock-badge stock-badge--${stockClass}">Giacenza: ${item.stock}</span>
                    ${(item.colors || []).length ? `<span style="font-size:var(--font-size-sm);color:var(--text-secondary);">Colori: ${_esc(item.colors.join(', '))}</span>` : ''}
                    ${(item.materials || []).length ? `<span style="font-size:var(--font-size-sm);color:var(--text-secondary);">Materiali: ${_esc(item.materials.join(', '))}</span>` : ''}
                </div>
            </div>
        </div>
    `;

    // Collapse back to preview
    wrapper.querySelector('.expanded-card__collapse-btn').addEventListener('click', (e) => {
        e.stopPropagation();
        _expandedIndex = null;
        _doRender();
    });

    // Edit popup
    wrapper.querySelector('.expanded-card__edit-btn').addEventListener('click', (e) => {
        e.stopPropagation();
        _openEditPopup(item, wrapper);
    });

    return wrapper;
}

// ---- Edit Popup ----

function _openEditPopup(item, cardEl) {
    const overlay = document.createElement('div');
    overlay.className = 'popup-overlay';
    overlay.innerHTML = `
        <div class="popup">
            <div class="popup__title">Modifica: ${_esc(item.article_name)}</div>
            <div class="popup__error" id="popup-error"></div>
            <div class="form-group">
                <label>Nome Articolo</label>
                <input class="form-control" name="article_name" value="${_esc(item.article_name)}">
            </div>
            <div class="form-group">
                <label>Descrizione</label>
                <textarea class="form-control" name="description">${_esc(item.description)}</textarea>
            </div>
            <div class="form-group">
                <label>Tags (separati da virgola)</label>
                <input class="form-control" name="tags" value="${_esc((item.tags || []).join(', '))}">
            </div>
            <div class="form-group">
                <label>Materiali (separati da virgola)</label>
                <input class="form-control" name="materials" value="${_esc((item.materials || []).join(', '))}">
            </div>
            <div class="popup__actions">
                <button class="btn-secondary" id="popup-cancel">Annulla</button>
                <button class="btn-primary" id="popup-save">Salva</button>
            </div>
        </div>`;

    document.body.appendChild(overlay);

    overlay.querySelector('#popup-cancel').addEventListener('click', () => overlay.remove());
    overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });

    overlay.querySelector('#popup-save').addEventListener('click', async () => {
        const saveBtn  = overlay.querySelector('#popup-save');
        const errorEl  = overlay.querySelector('#popup-error');
        errorEl.classList.remove('visible');
        saveBtn.disabled = true;
        saveBtn.textContent = 'Salvataggio...';

        const getVal = (name) => overlay.querySelector(`[name="${name}"]`)?.value?.trim() ?? '';
        const normalizeList = (raw) => raw ? raw.split(',').map(s => s.trim().toLowerCase()).filter(Boolean) : [];

        const fields = {};
        const name = getVal('article_name');
        const desc = getVal('description');
        const tags = normalizeList(getVal('tags'));
        const mats = normalizeList(getVal('materials'));

        if (name !== item.article_name) fields.article_name = name;
        if (desc !== item.description)  fields.description  = desc;
        if (JSON.stringify(tags) !== JSON.stringify(item.tags || [])) fields.tags = tags;
        if (JSON.stringify(mats) !== JSON.stringify(item.materials || [])) fields.materials = mats;

        if (Object.keys(fields).length === 0) {
            overlay.remove();
            return;
        }

        try {
            await api.patchCatalogItem(item.blueprint_id, fields);
            addMessage(`✅ Articolo "${item.article_name}" aggiornato.`, 'success');
            Object.assign(item, fields);
            overlay.remove();
            _doRender();
        } catch (err) {
            errorEl.textContent = err.message;
            errorEl.classList.add('visible');
            saveBtn.disabled = false;
            saveBtn.textContent = 'Salva';
        }
    });
}

// ---- Close panel ----
function _closePanel() {
    _expandedIndex = null;
    exitSplitScreen();
}
