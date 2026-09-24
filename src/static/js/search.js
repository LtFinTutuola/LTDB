/**
 * search.js
 * Handles the semantic search flow:
 *  - Submits a natural language query to POST /catalog/search/semantic
 *  - Renders results as an e-commerce-style grid in the detail panel
 *  - Opens a popup for quick inline edits via PATCH /catalog/{blueprint_id}
 */
import * as api from './api.js';
import {
    Mode, setMode,
    addMessage, enterSplitScreen, exitSplitScreen,
    getDetailPanel, _esc,
} from './main.js';

// ---- Public API ----

export async function executeSearch(query) {
    setMode(Mode.SEARCH);
    const loadingMsg = addMessage('<span class="spinner"></span>Ricerca in corso...', 'system');
    loadingMsg.innerHTML = '<span class="spinner"></span>Ricerca in corso...';

    try {
        const result = await api.semanticSearch(query);
        loadingMsg.remove();
        enterSplitScreen();
        addMessage(result.message, 'system');
        renderSearchResults(result.results || []);
    } catch (err) {
        loadingMsg.remove();
        addMessage(`❌ Ricerca fallita: ${err.message}`, 'error');
        setMode(Mode.IDLE);
    }
}

export function renderSearchResults(results) {
    const panel = getDetailPanel();

    if (!results || results.length === 0) {
        panel.innerHTML = `
            <div class="detail-panel__header">
                <div class="detail-panel__header-title-box">
                    <h2 class="detail-panel__title">Risultati ricerca</h2>
                </div>
                <button class="btn-close-panel" title="Chiudi">✕</button>
            </div>
            <div class="detail-panel__content">
                <div class="empty-state">
                    <span class="empty-state__icon">🔍</span>
                    <p class="empty-state__text">Nessun risultato trovato</p>
                </div>
                </div>
            </div>`;
        panel.querySelector('.btn-close-panel').addEventListener('click', exitSplitScreen);
        return;
    }

    const grid = results.map(_buildResultCard).join('');

    panel.innerHTML = `
        <div class="detail-panel__header">
            <div class="detail-panel__header-title-box">
                <h2 class="detail-panel__title">Risultati ricerca</h2>
                <span style="font-size:12px;color:var(--text-muted)">${results.length} articoli</span>
            </div>
            <button class="btn-close-panel" title="Chiudi">✕</button>
        </div>
        <div class="detail-panel__content">
            <div class="results-grid">${grid}</div>
        </div>`;

    panel.querySelector('.btn-close-panel').addEventListener('click', exitSplitScreen);

    // Attach edit button listeners
    panel.querySelectorAll('.result-card__edit-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const card = btn.closest('.result-card');
            const data = JSON.parse(card.dataset.item);
            _openEditPopup(data, card);
        });
    });
}

// ---- Card builder ----

function _buildResultCard(item) {
    const stockClass =
        item.stock > 5  ? 'available' :
        item.stock > 0  ? 'low' : 'zero';
    const stockLabel =
        item.stock > 0 ? `Giacenza: ${item.stock}` : 'Esaurito';

    const photoHtml = item.photo_id
        ? `<img src="${api.getPhotoUrl(item.photo_id)}" alt="${_esc(item.article_name)}"
               onerror="this.parentElement.innerHTML='<span class=&quot;result-card__photo-placeholder&quot;>🖼</span>'">`
        : `<span class="result-card__photo-placeholder">🖼</span>`;

    const tags = (item.tags || []).slice(0, 3)
        .map(t => `<span class="chip chip--small">${_esc(t)}</span>`).join('');

    // Encode item data for the popup
    const dataAttr = _esc(JSON.stringify(item));

    return `
    <div class="result-card" data-item="${dataAttr}">
        <div class="result-card__photo">${photoHtml}</div>
        <div class="result-card__info">
            <div class="result-card__name" title="${_esc(item.article_name)}">${_esc(item.article_name)}</div>
            <span class="result-card__brand">${_esc(item.brand_name)}</span>
            ${item.category_name ? `<span class="result-card__cat">${_esc(item.category_name)}</span>` : ''}
            <div class="result-card__tags">${tags}</div>
            <span class="stock-badge stock-badge--${stockClass}">${stockLabel}</span>
        </div>
        <button class="result-card__edit-btn" title="Modifica articolo">✏️</button>
    </div>`;
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

        // Build changed fields only
        const getVal = (name) => overlay.querySelector(`[name="${name}"]`)?.value?.trim() ?? '';
        const normalizeList = (raw) => raw ? raw.split(',').map(s => s.trim().toLowerCase()).filter(Boolean) : [];

        const fields = {};
        const name = getVal('article_name');
        const desc = getVal('description');
        const tags = normalizeList(getVal('tags'));
        const mats = normalizeList(getVal('materials'));

        if (name !== item.article_name) fields.article_name = name;
        if (desc !== item.description)  fields.description  = desc;
        const tagsChanged = JSON.stringify(tags) !== JSON.stringify(item.tags || []);
        const matsChanged = JSON.stringify(mats) !== JSON.stringify(item.materials || []);
        if (tagsChanged) fields.tags = tags;
        if (matsChanged) fields.materials = mats;

        if (Object.keys(fields).length === 0) {
            overlay.remove();
            return;
        }

        try {
            await api.patchCatalogItem(item.blueprint_id, fields);
            addMessage(`✅ Articolo "${item.article_name}" aggiornato.`, 'success');
            // Update card in place
            if (fields.article_name) {
                const nameEl = cardEl?.querySelector('.result-card__name');
                if (nameEl) nameEl.textContent = fields.article_name;
            }
            // Update cached data
            Object.assign(item, fields);
            try { cardEl.dataset.item = JSON.stringify(item); } catch {}
            overlay.remove();
        } catch (err) {
            errorEl.textContent = err.message;
            errorEl.classList.add('visible');
            saveBtn.disabled = false;
            saveBtn.textContent = 'Salva';
        }
    });
}
