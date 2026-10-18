import * as api from './api.js';

let _controlsBound = false;

export async function initWarehouse() {
    if (!_controlsBound) {
        _bindSearchControls();
        _controlsBound = true;
    }

    const searchInput = document.getElementById('warehouse-search-input');
    const stockToggle = document.getElementById('warehouse-stock-toggle');
    const query = searchInput ? searchInput.value.trim() : '';
    const inStockOnly = stockToggle ? stockToggle.checked : false;

    if (query) {
        await executeWarehouseSearch();
    } else {
        await fetchLatestItems(inStockOnly);
    }
}

function _esc(str) {
    if (typeof str !== 'string') return '';
    return str.replace(/[&<>"']/g, m => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;',
        '"': '&quot;', "'": '&#39;'
    })[m]);
}

function _renderResults(items, container, emptyMessage = 'Nessun articolo presente a magazzino.') {
    container.innerHTML = '';

    if (!items || items.length === 0) {
        container.innerHTML = `<div style="grid-column: 1/-1; text-align: center; padding: 40px; color: var(--text-secondary);">${emptyMessage}</div>`;
        return;
    }

    items.forEach(item => {
        const card = document.createElement('div');
        card.className = 'search-preview-card';
        card.style.cursor = 'pointer';

        const stockLabel = item.stock > 0 ? `Disponibile (${item.stock})` : 'Esaurito';
        const stockClass = item.stock > 5 ? 'available' : (item.stock > 0 ? 'low' : 'zero');

        const photoHtml = item.photo_id
            ? `<img src="${api.getPhotoUrl(item.photo_id)}" alt="${_esc(item.article_name)}"
                   onerror="this.parentElement.innerHTML='<span class=\\"preview-card__photo-placeholder\\">🖼</span>'">`
            : `<span class="preview-card__photo-placeholder">🖼</span>`;

        const tags = (item.tags || []).slice(0, 2)
            .map(t => `<span class="chip chip--small">${_esc(t)}</span>`).join('');

        card.innerHTML = `
            <div class="preview-card__photo">${photoHtml}</div>
            <div class="preview-card__info">
                <div class="preview-card__name" title="${_esc(item.article_name)}">${_esc(item.article_name)}</div>
                <span class="preview-card__brand">${_esc(item.brand_name)}</span>
                ${item.category_name ? `<span class="preview-card__cat">${_esc(item.category_name)}</span>` : ''}
                <div class="preview-card__tags">${tags}</div>
                <span class="stock-badge stock-badge--${stockClass}">${stockLabel}</span>
            </div>
        `;

        card.addEventListener('click', () => {
            showArticleFullscreen(item);
        });

        container.appendChild(card);
    });
}

async function fetchLatestItems(inStockOnly = false) {
    const container = document.getElementById('catalog-results');
    if (!container) return;

    try {
        const res = await fetch('/api/v1/catalog/items?limit=50&offset=0');
        if (!res.ok) throw new Error('Failed to fetch items');
        const data = await res.json();

        let items = data.results || [];
        if (inStockOnly) {
            items = items.filter(it => (it.stock || 0) > 0);
        }

        _renderResults(items, container, inStockOnly ? 'Nessun articolo con giacenza disponibile.' : 'Nessun articolo presente a magazzino.');
    } catch (e) {
        console.error(e);
        container.innerHTML = '<div style="grid-column: 1/-1; text-align: center; padding: 40px; color: var(--error);">Errore nel caricamento del magazzino.</div>';
    }
}

async function executeWarehouseSearch() {
    const container = document.getElementById('catalog-results');
    const searchInput = document.getElementById('warehouse-search-input');
    const searchBtn = document.getElementById('warehouse-search-btn');
    const clearBtn = document.getElementById('warehouse-search-clear');
    const stockToggle = document.getElementById('warehouse-stock-toggle');
    const feedbackEl = document.getElementById('warehouse-search-feedback');

    if (!container || !searchInput) return;

    const query = searchInput.value.trim();
    const inStockOnly = stockToggle ? Boolean(stockToggle.checked) : false;

    if (!query) {
        if (clearBtn) clearBtn.style.display = 'none';
        if (feedbackEl) {
            feedbackEl.style.display = 'none';
            feedbackEl.textContent = '';
        }
        await fetchLatestItems(inStockOnly);
        return;
    }

    if (clearBtn) clearBtn.style.display = 'inline-flex';

    // Show loading state
    container.innerHTML = `
        <div style="grid-column: 1/-1; text-align: center; padding: 50px 20px; color: var(--text-secondary);">
            <div style="font-size: 16px; margin-bottom: 8px;">🔍 Ricerca in corso...</div>
            <div style="font-size: 13px; color: var(--text-muted);">Elaborazione semantica e filtri di magazzino</div>
        </div>
    `;

    if (searchBtn) searchBtn.disabled = true;
    searchInput.disabled = true;

    try {
        const response = await api.semanticSearch(query, inStockOnly);
        const results = response.results || [];

        if (feedbackEl) {
            feedbackEl.style.display = 'block';
            feedbackEl.textContent = response.message || `Trovati ${results.length} articoli`;
        }

        _renderResults(
            results,
            container,
            `Nessun articolo trovato per la ricerca «${_esc(query)}»${inStockOnly ? ' con giacenza disponibile' : ''}.`
        );
    } catch (err) {
        console.error('Search error:', err);
        container.innerHTML = `<div style="grid-column: 1/-1; text-align: center; padding: 40px; color: var(--error);">Errore durante la ricerca: ${_esc(err.message || 'Errore del server')}</div>`;
        if (feedbackEl) {
            feedbackEl.style.display = 'block';
            feedbackEl.style.color = 'var(--error)';
            feedbackEl.textContent = 'Errore durante l\'esecuzione della ricerca.';
        }
    } finally {
        if (searchBtn) searchBtn.disabled = false;
        searchInput.disabled = false;
        searchInput.focus();
    }
}

function _bindSearchControls() {
    const searchInput = document.getElementById('warehouse-search-input');
    const searchBtn = document.getElementById('warehouse-search-btn');
    const clearBtn = document.getElementById('warehouse-search-clear');
    const stockToggle = document.getElementById('warehouse-stock-toggle');
    const feedbackEl = document.getElementById('warehouse-search-feedback');

    if (searchInput) {
        searchInput.addEventListener('input', () => {
            const hasText = searchInput.value.trim().length > 0;
            if (clearBtn) clearBtn.style.display = hasText ? 'inline-flex' : 'none';
            if (!hasText) {
                if (feedbackEl) {
                    feedbackEl.style.display = 'none';
                    feedbackEl.textContent = '';
                }
                const inStockOnly = stockToggle ? Boolean(stockToggle.checked) : false;
                fetchLatestItems(inStockOnly);
            }
        });

        searchInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                executeWarehouseSearch();
            }
        });
    }

    if (searchBtn) {
        searchBtn.addEventListener('click', () => {
            executeWarehouseSearch();
        });
    }

    if (clearBtn) {
        clearBtn.addEventListener('click', () => {
            if (searchInput) {
                searchInput.value = '';
                searchInput.focus();
            }
            clearBtn.style.display = 'none';
            if (feedbackEl) {
                feedbackEl.style.display = 'none';
                feedbackEl.textContent = '';
            }
            const inStockOnly = stockToggle ? Boolean(stockToggle.checked) : false;
            fetchLatestItems(inStockOnly);
        });
    }

    if (stockToggle) {
        stockToggle.addEventListener('change', () => {
            const query = searchInput ? searchInput.value.trim() : '';
            if (query) {
                executeWarehouseSearch();
            } else {
                fetchLatestItems(Boolean(stockToggle.checked));
            }
        });
    }
}

function showArticleFullscreen(item) {
    let overlay = document.getElementById('article-fullscreen-modal');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'article-fullscreen-modal';
        overlay.style.position = 'fixed';
        overlay.style.top = '0';
        overlay.style.left = '0';
        overlay.style.width = '100vw';
        overlay.style.height = '100vh';
        overlay.style.backgroundColor = 'rgba(0,0,0,0.85)';
        overlay.style.zIndex = '10000';
        overlay.style.display = 'none';
        overlay.style.justifyContent = 'center';
        overlay.style.alignItems = 'center';
        overlay.style.backdropFilter = 'blur(4px)';
        document.body.appendChild(overlay);

        overlay.addEventListener('click', (e) => {
            if (e.target === overlay || e.target.closest('.close-article-modal')) {
                overlay.style.display = 'none';
            }
        });
    }

    const photoHtml = item.photo_id
        ? `<div style="width:100%; height:100%; display:flex; justify-content:center; align-items:center; overflow:hidden; border-radius:8px;">
               <img src="${api.getPhotoUrl(item.photo_id)}" style="width:100%; height:100%; max-height:60vh; object-fit:contain; border-radius:8px;" alt="${_esc(item.article_name)}" onerror="this.style.display='none'">
           </div>`
        : `<div style="width:100%; height:300px; display:flex; align-items:center; justify-content:center; font-size:48px; background:var(--bg-elevated); border-radius:8px;">🖼</div>`;

    const tags = (item.tags || []).map(t => `<span class="chip">${_esc(t)}</span>`).join('');

    const stockLabel = item.stock > 0 ? `Disponibile (${item.stock})` : 'Esaurito';
    const stockClass = item.stock > 5 ? 'available' : (item.stock > 0 ? 'low' : 'zero');

    overlay.innerHTML = `
        <div style="background: var(--bg-surface); width: 90%; max-width: 900px; max-height: 90vh; overflow-y: auto; border-radius: 12px; position: relative; display: flex; flex-direction: column; box-shadow: 0 10px 30px rgba(0,0,0,0.5);">
            <div style="display: flex; justify-content: space-between; align-items: center; padding: 20px; border-bottom: 1px solid var(--border-color);">
                <h2 style="margin: 0; font-size: 20px;">Dettaglio Articolo</h2>
                <button class="close-article-modal" style="background: none; border: none; cursor: pointer; color: var(--text-secondary); display:flex; align-items:center;">
                    <span class="material-symbols-rounded" style="font-size: 24px;">close</span>
                </button>
            </div>
            <div style="display: flex; flex-direction: row; gap: 30px; padding: 30px; flex-wrap: wrap;">
                <div style="flex: 1; text-align: center; min-width: 250px; max-width: 400px; margin: 0 auto; aspect-ratio: 3/4;">
                    ${photoHtml}
                </div>
                <div style="flex: 1; display: flex; flex-direction: column; gap: 15px; min-width: 300px;">
                    <div>
                        <h1 style="margin: 0 0 5px 0; font-size: 24px; color: var(--text-primary);">${_esc(item.article_name)}</h1>
                        <div style="font-size: 18px; color: var(--accent); font-weight: 500;">${_esc(item.brand_name)}</div>
                    </div>
                    
                    <div style="display: flex; gap: 10px; flex-wrap: wrap; margin-top: 10px;">
                        ${tags}
                    </div>

                    ${item.description ? `<div style="font-size: 14px; color: var(--text-primary); margin-top: 5px;"><strong>Descrizione Breve:</strong><br>${_esc(item.description)}</div>` : ''}
                    ${item.extended_description ? `<div style="font-size: 14px; color: var(--text-secondary); margin-top: 5px; white-space: pre-wrap;"><strong>Descrizione Estesa:</strong><br>${_esc(item.extended_description)}</div>` : ''}

                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 15px; background: var(--bg-app); padding: 15px; border-radius: 8px; margin-top: 10px;">
                        <div>
                            <div style="font-size: 12px; color: var(--text-secondary);">Categoria</div>
                            <div style="font-weight: 500; font-size: 14px;">${item.category_name ? _esc(item.category_name) : 'N/D'}</div>
                        </div>
                        <div>
                            <div style="font-size: 12px; color: var(--text-secondary);">Sotto-categoria</div>
                            <div style="font-weight: 500; font-size: 14px;">${item.sub_category_name ? _esc(item.sub_category_name) : 'N/D'}</div>
                        </div>
                        <div>
                            <div style="font-size: 12px; color: var(--text-secondary);">Barcode</div>
                            <div style="font-weight: 500; font-size: 14px;">${item.barcode ? _esc(item.barcode) : 'N/D'}</div>
                        </div>
                        <div>
                            <div style="font-size: 12px; color: var(--text-secondary);">Codice Fornitore</div>
                            <div style="font-weight: 500; font-size: 14px;">${item.vendor_code ? _esc(item.vendor_code) : 'N/D'}</div>
                        </div>
                        <div>
                            <div style="font-size: 12px; color: var(--text-secondary);">Giacenza</div>
                            <div><span class="stock-badge stock-badge--${stockClass}">${stockLabel}</span></div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    `;
    overlay.style.display = 'flex';
}
