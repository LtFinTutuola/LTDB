import * as api from './api.js?v=20261022_6';
import { setupBarcodeScanner } from './barcode_scanner.js?v=20261022_6';
import { requestPhotoUpload } from './photo_upload_helper.js?v=20261022_6';

let _controlsBound = false;
let _editingItem = null;
let _deletingItem = null;
let _currentMobileArticle = null;
let _currentPage = 1;
const _pageSize = 50;
let _totalItems = 0;
let _totalBlueprints = 0;
let _totalPages = 1;

export async function initWarehouse() {
    if (!_controlsBound) {
        _bindSearchControls();
        _bindArticleModals();
        _controlsBound = true;
    }

    await _refreshWarehouseView();
}

async function _refreshWarehouseView() {
    const searchInput = document.getElementById('warehouse-search-input');
    const stockToggle = document.getElementById('warehouse-stock-toggle');
    const query = searchInput ? searchInput.value.trim() : '';
    const inStockOnly = stockToggle ? Boolean(stockToggle.checked) : false;

    if (query) {
        await executeWarehouseSearch();
    } else {
        await fetchLatestItems(inStockOnly, _currentPage);
    }
}

function _esc(str) {
    if (typeof str !== 'string') return '';
    return str.replace(/[&<>"']/g, m => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;',
        '"': '&quot;', "'": '&#39;'
    })[m]);
}

function _showToast(message, type = 'success') {
    const toast = document.createElement('div');
    toast.style.position = 'fixed';
    toast.style.bottom = '24px';
    toast.style.right = '24px';
    toast.style.padding = '12px 20px';
    toast.style.borderRadius = 'var(--radius-sm)';
    toast.style.background = type === 'error' ? 'var(--error)' : 'var(--bg-elevated)';
    toast.style.color = '#fff';
    toast.style.border = type === 'error' ? '1px solid var(--error)' : '1px solid var(--border-color)';
    toast.style.boxShadow = '0 8px 16px rgba(0,0,0,0.3)';
    toast.style.zIndex = '13000';
    toast.style.fontSize = '13px';
    toast.style.display = 'flex';
    toast.style.alignItems = 'center';
    toast.style.gap = '8px';
    toast.style.transition = 'opacity 0.25s ease';

    const icon = document.createElement('span');
    icon.className = 'material-symbols-rounded';
    icon.style.fontSize = '18px';
    icon.textContent = type === 'error' ? 'error' : 'check_circle';
    toast.appendChild(icon);

    const text = document.createElement('span');
    text.textContent = message;
    toast.appendChild(text);

    document.body.appendChild(toast);

    setTimeout(() => {
        toast.style.opacity = '0';
        setTimeout(() => toast.remove(), 250);
    }, 3500);
}

function _extractBaseArticleName(item) {
    const rawName = item.article_name || '';
    const color = item.colors && item.colors.length > 0 ? item.colors[0] : null;
    if (color && color.toLowerCase() !== 'unknown') {
        const suffix = ` - ${color}`;
        if (rawName.toLowerCase().endsWith(suffix.toLowerCase())) {
            return rawName.slice(0, rawName.length - suffix.length).trim();
        }
    }
    return rawName;
}

function _renderResults(items, container, emptyMessage = 'Nessun articolo presente a magazzino.') {
    container.innerHTML = '';

    if (!items || items.length === 0) {
        container.innerHTML = `
            <div class="pim-empty-state" style="padding: 48px 20px;">
                <span class="material-symbols-rounded">inventory_2</span>
                <p>${emptyMessage}</p>
            </div>
        `;
        return;
    }

    const table = document.createElement('table');
    table.className = 'warehouse-table';
    table.innerHTML = `
        <thead>
            <tr>
                <th style="width: 68px;">Foto</th>
                <th>Articolo</th>
                <th style="width: 140px;">Brand</th>
                <th style="width: 180px;">Categoria</th>
                <th style="width: 165px;">Codici</th>
                <th>Tag &amp; Materiali</th>
                <th style="width: 135px;">Giacenza</th>
                <th style="width: 90px; text-align: right;">Azioni</th>
            </tr>
        </thead>
        <tbody></tbody>
    `;

    const tbody = table.querySelector('tbody');

    items.forEach(item => {
        const tr = document.createElement('tr');
        tr.className = 'warehouse-row';

        const stockLabel = item.stock > 0 ? `Disponibile (${item.stock})` : 'Esaurito';
        const stockClass = item.stock > 5 ? 'available' : (item.stock > 0 ? 'low' : 'zero');

        const hasPhoto = Boolean(item.photo_id);
        const photoUrl = hasPhoto ? api.getPhotoUrl(item.photo_id) : '';
        const photoHtml = hasPhoto
            ? `<img src="${photoUrl}" alt=""
                   onerror="this.parentElement.classList.remove('warehouse-row__photo--zoomable'); this.parentElement.innerHTML='<span class=\\"warehouse-row__photo-placeholder\\">🖼</span>'">`
            : `<span class="warehouse-row__photo-placeholder">🖼</span>`;

        // Category & Subcategory formatting
        let categoryHtml = `<span style="color: var(--text-muted); font-style: italic;">-</span>`;
        if (item.category_name && item.sub_category_name) {
            categoryHtml = `
                <div style="font-weight: 500; color: var(--text-primary);">${_esc(item.category_name)}</div>
                <div style="font-size: 11px; color: var(--text-muted); margin-top: 2px;">↳ ${_esc(item.sub_category_name)}</div>
            `;
        } else if (item.category_name) {
            categoryHtml = `<div style="font-weight: 500; color: var(--text-primary);">${_esc(item.category_name)}</div>`;
        } else if (item.sub_category_name) {
            categoryHtml = `<div style="font-weight: 500; color: var(--text-primary);">${_esc(item.sub_category_name)}</div>`;
        }

        // Codes formatting
        const codeParts = [];
        if (item.vendor_code) {
            codeParts.push(`<div class="warehouse-row__code" title="Codice Fornitore"><span style="color: var(--text-muted);">Cod:</span> ${_esc(item.vendor_code)}</div>`);
        }
        if (item.barcode) {
            codeParts.push(`<div class="warehouse-row__code" title="Barcode EAN"><span style="color: var(--text-muted);">EAN:</span> ${_esc(item.barcode)}</div>`);
        }
        const codesHtml = codeParts.length > 0
            ? codeParts.join('')
            : `<span style="color: var(--text-muted); font-style: italic;">-</span>`;

        // Tags & Materials chips
        const combinedChips = [...(item.tags || []), ...(item.materials || [])];
        let chipsHtml = `<span style="color: var(--text-muted); font-style: italic;">-</span>`;
        if (combinedChips.length > 0) {
            const visibleChips = combinedChips.slice(0, 3)
                .map(c => `<span class="mini-chip">${_esc(c)}</span>`)
                .join('');
            const moreBadge = combinedChips.length > 3
                ? `<span class="mini-chip mini-chip--more">+${combinedChips.length - 3}</span>`
                : '';
            chipsHtml = `<div class="tags-list">${visibleChips}${moreBadge}</div>`;
        }

        let catText = item.category_name || '';
        if (item.sub_category_name) {
            catText = catText ? `${catText} ↳ ${item.sub_category_name}` : item.sub_category_name;
        }

        tr.innerHTML = `
            <td class="warehouse-row__cell--photo">
                <div class="warehouse-row__photo ${hasPhoto ? 'warehouse-row__photo--zoomable' : ''}">
                    ${photoHtml}
                </div>
            </td>
            <td class="warehouse-row__cell--info">
                <div class="warehouse-row__mobile-meta-top">
                    <span class="warehouse-row__mobile-brand">${_esc(item.brand_name || '')}</span>
                    ${catText ? `<span class="warehouse-row__mobile-cat-divider">•</span><span class="warehouse-row__mobile-category">${_esc(catText)}</span>` : ''}
                </div>
                <div class="warehouse-row__title" title="${_esc(item.article_name)}">${_esc(item.article_name)}</div>
                ${item.description ? `<div class="warehouse-row__desc warehouse-desktop-only" title="${_esc(item.description)}">${_esc(item.description)}</div>` : ''}
                <div class="warehouse-row__mobile-meta-bottom">
                    <span class="stock-badge stock-badge--${stockClass}">${stockLabel}</span>
                </div>
            </td>
            <td class="warehouse-desktop-only">
                <span style="color: var(--accent); font-weight: 500;">${_esc(item.brand_name)}</span>
            </td>
            <td class="warehouse-desktop-only">${categoryHtml}</td>
            <td class="warehouse-desktop-only">${codesHtml}</td>
            <td class="warehouse-desktop-only">${chipsHtml}</td>
            <td class="warehouse-desktop-only">
                <span class="stock-badge stock-badge--${stockClass}">${stockLabel}</span>
            </td>
            <td class="warehouse-desktop-only" style="text-align: right;">
                <div class="action-buttons" style="justify-content: flex-end;">
                    <button type="button" class="action-btn btn-edit-article" title="Modifica Articolo">
                        <span class="material-symbols-rounded" style="font-size: 18px;">edit</span>
                    </button>
                    <button type="button" class="action-btn action-btn--delete btn-delete-article" title="Elimina Articolo">
                        <span class="material-symbols-rounded" style="font-size: 18px;">delete</span>
                    </button>
                </div>
            </td>
        `;

        // Photo click -> fullscreen image (desktop only; on mobile entire row navigates to detail panel)
        const photoEl = tr.querySelector('.warehouse-row__photo');
        if (photoEl && hasPhoto) {
            photoEl.addEventListener('click', (e) => {
                if (window.innerWidth > 768) {
                    e.stopPropagation();
                    showImageFullscreen(photoUrl, item.article_name);
                }
            });
        }

        // Edit button click -> edit modal
        const editBtn = tr.querySelector('.btn-edit-article');
        if (editBtn) {
            editBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                openEditArticleModal(item);
            });
        }

        // Delete button click -> delete modal
        const deleteBtn = tr.querySelector('.btn-delete-article');
        if (deleteBtn) {
            deleteBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                openDeleteArticleModal(item);
            });
        }

        // Row click -> mobile navigation panel or fullscreen article detail
        tr.addEventListener('click', () => {
            if (window.innerWidth <= 768) {
                openArticleMobilePanel(item);
            } else {
                showArticleFullscreen(item);
            }
        });

        tbody.appendChild(tr);
    });

    container.appendChild(table);
}

async function fetchLatestItems(inStockOnly = false, page = 1) {
    _currentPage = Math.max(1, page);
    const container = document.getElementById('catalog-results');
    if (!container) return;

    try {
        const offset = (_currentPage - 1) * _pageSize;
        const data = await api.getCatalogItems(_pageSize, offset, inStockOnly);

        let items = data.results || [];
        _totalItems = typeof data.total_items === 'number' ? data.total_items : (typeof data.total_blueprints === 'number' ? data.total_blueprints : items.length);
        _totalBlueprints = typeof data.total_blueprints === 'number' ? data.total_blueprints : 0;
        _totalPages = Math.max(1, Math.ceil(_totalItems / _pageSize));

        if (_currentPage > _totalPages && _totalItems > 0) {
            _currentPage = _totalPages;
            return await fetchLatestItems(inStockOnly, _currentPage);
        }

        _renderResults(
            items,
            container,
            inStockOnly ? 'Nessun articolo con giacenza disponibile.' : 'Nessun articolo presente a magazzino.'
        );

        _renderPagination(container, inStockOnly);
    } catch (e) {
        console.error('fetchLatestItems error:', e);
        container.innerHTML = '<div style="text-align: center; padding: 40px; color: var(--error);">Errore nel caricamento del magazzino.</div>';
    }
}

function _renderPagination(container, inStockOnly) {
    if (!container) return;

    // Clean up any existing pagination elements
    container.querySelectorAll('.warehouse-pagination').forEach(el => el.remove());
    document.querySelectorAll('.warehouse-pagination').forEach(el => el.remove());

    if (_totalItems <= 0) return;

    const paginationEl = document.createElement('div');
    paginationEl.id = 'warehouse-pagination';
    paginationEl.className = 'warehouse-pagination';

    const startItem = (_currentPage - 1) * _pageSize + 1;
    const endItem = Math.min(_currentPage * _pageSize, _totalItems);

    const bpInfo = _totalBlueprints > 0 && _totalBlueprints !== _totalItems 
        ? ` (${_totalBlueprints} modelli)` 
        : '';

    let html = `
        <div class="warehouse-pagination__info">
            Mostrando <strong>${startItem}-${endItem}</strong> di <strong>${_totalItems}</strong> articoli${bpInfo}
            &nbsp;•&nbsp; Pagina <strong>${_currentPage}</strong> di <strong>${_totalPages}</strong>
        </div>
        <div class="warehouse-pagination__controls">
            <button type="button" class="warehouse-pagination__btn" data-page="PREV" title="Pagina precedente" ${_currentPage <= 1 ? 'disabled' : ''}>
                <span class="material-symbols-rounded" style="font-size: 18px;">chevron_left</span>
                <span class="warehouse-pagination__btn-label">Precedente</span>
            </button>
    `;

    const pages = [];
    if (_totalPages <= 7) {
        for (let i = 1; i <= _totalPages; i++) pages.push(i);
    } else {
        pages.push(1);
        if (_currentPage > 3) {
            pages.push('...');
        }
        const start = Math.max(2, _currentPage - 1);
        const end = Math.min(_totalPages - 1, _currentPage + 1);
        for (let i = start; i <= end; i++) {
            pages.push(i);
        }
        if (_currentPage < _totalPages - 2) {
            pages.push('...');
        }
        pages.push(_totalPages);
    }

    pages.forEach(p => {
        if (p === '...') {
            html += `<span class="warehouse-pagination__ellipsis">…</span>`;
        } else {
            const isActive = p === _currentPage;
            html += `
                <button type="button" class="warehouse-pagination__btn ${isActive ? 'warehouse-pagination__btn--active' : ''}" data-page="${p}" ${isActive ? 'aria-current="page" disabled' : ''}>
                    ${p}
                </button>
            `;
        }
    });

    html += `
            <button type="button" class="warehouse-pagination__btn" data-page="NEXT" title="Pagina successiva" ${_currentPage >= _totalPages ? 'disabled' : ''}>
                <span class="warehouse-pagination__btn-label">Successivo</span>
                <span class="material-symbols-rounded" style="font-size: 18px;">chevron_right</span>
            </button>
        </div>
    `;

    paginationEl.innerHTML = html;

    // Robust event delegation on the pagination container
    paginationEl.addEventListener('click', async (e) => {
        const btn = e.target.closest('.warehouse-pagination__btn');
        if (!btn) return;
        if (btn.disabled || btn.hasAttribute('disabled') || btn.classList.contains('warehouse-pagination__btn--active')) {
            return;
        }

        e.preventDefault();
        e.stopPropagation();

        const targetPageStr = btn.dataset.page;
        let targetPage = _currentPage;
        if (targetPageStr === 'PREV') {
            targetPage = _currentPage - 1;
        } else if (targetPageStr === 'NEXT') {
            targetPage = _currentPage + 1;
        } else {
            targetPage = parseInt(targetPageStr, 10);
        }

        if (!isNaN(targetPage) && targetPage >= 1 && targetPage <= _totalPages && targetPage !== _currentPage) {
            const stockToggle = document.getElementById('warehouse-stock-toggle');
            const currentInStockOnly = stockToggle ? Boolean(stockToggle.checked) : inStockOnly;

            container.scrollTop = 0;
            await fetchLatestItems(currentInStockOnly, targetPage);
        }
    });

    container.appendChild(paginationEl);
}

async function executeWarehouseSearch() {
    const container = document.getElementById('catalog-results');
    const searchInput = document.getElementById('warehouse-search-input');
    const searchBtn = document.getElementById('warehouse-search-btn');
    const clearBtn = document.getElementById('warehouse-search-clear');
    const stockToggle = document.getElementById('warehouse-stock-toggle');
    const feedbackEl = document.getElementById('warehouse-search-feedback');

    if (!container || !searchInput) return;

    // Clean up any pagination elements during search
    container.querySelectorAll('.warehouse-pagination').forEach(el => el.remove());
    document.querySelectorAll('.warehouse-pagination').forEach(el => el.remove());

    const query = searchInput.value.trim();
    const inStockOnly = stockToggle ? Boolean(stockToggle.checked) : false;

    if (!query) {
        if (clearBtn) clearBtn.style.display = 'none';
        if (feedbackEl) {
            feedbackEl.style.display = 'none';
            feedbackEl.textContent = '';
        }
        _currentPage = 1;
        await fetchLatestItems(inStockOnly, 1);
        return;
    }

    if (clearBtn) clearBtn.style.display = 'inline-flex';

    // Show loading state
    container.innerHTML = `
        <div class="pim-loading-state" style="padding: 50px 20px;">
            <div class="pim-loading-spinner"></div>
            <div style="font-size: 15px; color: var(--text-primary); margin-top: 4px;">Ricerca in corso...</div>
            <div style="font-size: 12px; color: var(--text-muted);">Elaborazione semantica e filtri di magazzino</div>
        </div>
    `;

    if (searchBtn) searchBtn.disabled = true;
    searchInput.disabled = true;

    try {
        const response = await api.semanticSearch(query, inStockOnly);
        const results = response.results || [];

        if (feedbackEl) {
            feedbackEl.style.display = 'block';
            feedbackEl.style.color = 'var(--text-secondary)';
            feedbackEl.textContent = response.message || `Trovati ${results.length} articoli`;
        }

        _renderResults(
            results,
            container,
            `Nessun articolo trovato per la ricerca «${_esc(query)}»${inStockOnly ? ' con giacenza disponibile' : ''}.`
        );
    } catch (err) {
        console.error('Search error:', err);
        container.innerHTML = `<div style="text-align: center; padding: 40px; color: var(--error);">Errore durante la ricerca: ${_esc(err.message || 'Errore del server')}</div>`;
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
                _currentPage = 1;
                fetchLatestItems(inStockOnly, 1);
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
            _currentPage = 1;
            fetchLatestItems(inStockOnly, 1);
        });
    }

    if (stockToggle) {
        stockToggle.addEventListener('change', () => {
            const query = searchInput ? searchInput.value.trim() : '';
            if (query) {
                executeWarehouseSearch();
            } else {
                _currentPage = 1;
                fetchLatestItems(Boolean(stockToggle.checked), 1);
            }
        });
    }

    // Camera Barcode Scanner for warehouse search bar (works on HTTP without SSL)
    setupBarcodeScanner({
        cameraInputId: 'warehouse-barcode-camera-input',
        triggerBtnIds: ['warehouse-barcode-scan-btn', 'warehouse-search-scan-btn'],
        onSuccess: (barcode) => {
            if (searchInput) {
                searchInput.value = barcode;
                if (clearBtn) clearBtn.style.display = 'inline-flex';
                executeWarehouseSearch();
            }
        },
        onError: () => {},
        showToastFn: _showToast,
    });

    // Camera Barcode Scanner for mobile article edit panel
    const btnScanMobArt = document.getElementById('btn-scan-mob-art-barcode');
    if (btnScanMobArt) {
        setupBarcodeScanner({
            cameraInputId: 'mob-art-barcode-camera-input',
            triggerBtnIds: ['btn-scan-mob-art-barcode'],
            onSuccess: (barcode) => {
                const mobInput = document.getElementById('mob-art-barcode');
                if (mobInput) {
                    mobInput.value = barcode;
                }
            },
            onError: () => {},
            showToastFn: _showToast,
        });
    }
}

// ============================================================================
// Edit & Delete Modals Logic
// ============================================================================

function _bindArticleModals() {
    const modalEdit = document.getElementById('modal-edit-article');
    const formEdit = document.getElementById('form-edit-article');
    const btnCloseEdit = document.getElementById('btn-close-edit-article');
    const btnCancelEdit = document.getElementById('btn-cancel-edit-article');

    const modalDelete = document.getElementById('modal-delete-article');
    const btnCloseDelete = document.getElementById('btn-close-delete-article');
    const btnCancelDelete = document.getElementById('btn-cancel-delete-article');
    const btnConfirmDelete = document.getElementById('btn-confirm-delete-article');

    if (btnCloseEdit) btnCloseEdit.addEventListener('click', closeEditArticleModal);
    if (btnCancelEdit) btnCancelEdit.addEventListener('click', closeEditArticleModal);
    if (formEdit) formEdit.addEventListener('submit', _handleSaveEditArticle);
    if (modalEdit) {
        modalEdit.addEventListener('click', (e) => {
            if (e.target === modalEdit) closeEditArticleModal();
        });
    }

    if (btnCloseDelete) btnCloseDelete.addEventListener('click', closeDeleteArticleModal);
    if (btnCancelDelete) btnCancelDelete.addEventListener('click', closeDeleteArticleModal);
    if (btnConfirmDelete) btnConfirmDelete.addEventListener('click', _handleConfirmDeleteArticle);
    if (modalDelete) {
        modalDelete.addEventListener('click', (e) => {
            if (e.target === modalDelete) closeDeleteArticleModal();
        });
    }

    // Mobile Article Panel Controls
    const btnBackMobile = document.getElementById('btn-article-panel-back');
    const btnDeleteMobile = document.getElementById('btn-article-panel-delete');
    const formMobile = document.getElementById('form-article-mobile');
    const photoWrapMobile = document.getElementById('mob-art-photo-wrap');

    if (btnBackMobile) btnBackMobile.addEventListener('click', closeArticleMobilePanel);
    if (btnDeleteMobile) {
        btnDeleteMobile.addEventListener('click', () => {
            if (_currentMobileArticle) {
                openDeleteArticleModal(_currentMobileArticle);
            }
        });
    }
    if (formMobile) formMobile.addEventListener('submit', _handleSaveArticleMobile);
    if (photoWrapMobile) {
        photoWrapMobile.addEventListener('click', () => {
            if (_currentMobileArticle && _currentMobileArticle.photo_id) {
                showImageFullscreen(api.getPhotoUrl(_currentMobileArticle.photo_id), _currentMobileArticle.article_name);
            }
        });
    }

    // Desktop photo buttons
    const btnUploadDesktop = document.getElementById('btn-edit-art-upload-photo');
    if (btnUploadDesktop) {
        btnUploadDesktop.addEventListener('click', () => {
            if (!_editingItem) return;
            const color = _editingItem.colors && _editingItem.colors.length > 0 ? _editingItem.colors[0] : '';
            requestPhotoUpload({
                onSuccess: async (compressedBlob) => {
                    // Instant local preview
                    const blobUrl = URL.createObjectURL(compressedBlob);
                    const img = document.getElementById('edit-art-photo');
                    const placeholder = document.getElementById('edit-art-photo-placeholder');
                    if (img && placeholder) {
                        img.src = blobUrl;
                        img.style.display = 'block';
                        placeholder.style.display = 'none';
                    }
                    _showToast('Caricamento foto in corso...', 'info');
                    try {
                        const res = await api.uploadCatalogPhoto(_editingItem.blueprint_id, compressedBlob, color);
                        if (res && res.photo_id) {
                            api.invalidatePhotoCache(res.photo_id);
                            _editingItem.photo_id = res.photo_id;
                            _updateArticlePhotoPreview('desktop', res.photo_id, true);
                            _showToast('Foto aggiornata con successo!');
                            await _refreshWarehouseView();
                        }
                    } catch (err) {
                        _showToast(err.message || 'Errore durante il caricamento della foto.', 'error');
                        _updateArticlePhotoPreview('desktop', _editingItem.photo_id, false);
                    }
                },
                onError: (err) => {
                    _showToast('Errore durante la selezione della foto: ' + (err.message || err), 'error');
                }
            });
        });
    }

    const btnGoogleDesktop = document.getElementById('btn-edit-art-search-google');
    if (btnGoogleDesktop) {
        btnGoogleDesktop.addEventListener('click', () => {
            if (!_editingItem) return;
            const color = _editingItem.colors && _editingItem.colors.length > 0 ? _editingItem.colors[0] : '';
            const q = `${_editingItem.brand_name || ''} ${_extractBaseArticleName(_editingItem)} ${color}`.trim();
            const searchUrl = `https://www.google.com/search?tbm=isch&q=${encodeURIComponent(q)}`;
            window.open(searchUrl, '_blank', 'noopener,noreferrer');
        });
    }

    const btnUrlDesktop = document.getElementById('btn-edit-art-manual-url');
    if (btnUrlDesktop) {
        btnUrlDesktop.addEventListener('click', async () => {
            if (!_editingItem) return;
            const url = prompt('Inserisci l\'URL dell\'immagine da impostare:');
            if (!url || !url.trim()) return;
            const color = _editingItem.colors && _editingItem.colors.length > 0 ? _editingItem.colors[0] : '';
            _showToast('Download foto in corso...', 'info');
            try {
                const res = await api.setCatalogPhotoUrl(_editingItem.blueprint_id, url.trim(), color);
                if (res && res.photo_id) {
                    api.invalidatePhotoCache(res.photo_id);
                    _editingItem.photo_id = res.photo_id;
                    _updateArticlePhotoPreview('desktop', res.photo_id, true);
                    _showToast('Foto impostata con successo!');
                    await _refreshWarehouseView();
                }
            } catch (err) {
                _showToast(err.message || 'Impossibile scaricare la foto dall\'URL.', 'error');
            }
        });
    }

    const btnAiDesktop = document.getElementById('btn-edit-art-ai-search');
    if (btnAiDesktop) {
        btnAiDesktop.addEventListener('click', async () => {
            if (!_editingItem) return;
            const feedback = prompt('Descrivi la foto da cercare su Internet con l\'AI (opzionale):');
            const color = _editingItem.colors && _editingItem.colors.length > 0 ? _editingItem.colors[0] : '';
            _showToast('Ricerca AI su Internet in corso...', 'info');
            btnAiDesktop.disabled = true;
            try {
                const res = await api.searchCatalogPhoto(_editingItem.blueprint_id, color, feedback || null);
                if (res && res.photo_id) {
                    api.invalidatePhotoCache(res.photo_id);
                    _editingItem.photo_id = res.photo_id;
                    _updateArticlePhotoPreview('desktop', res.photo_id, true);
                    _showToast('Foto trovata e impostata con successo!');
                    await _refreshWarehouseView();
                }
            } catch (err) {
                _showToast(err.message || 'Nessuna foto trovata con l\'AI.', 'error');
            } finally {
                btnAiDesktop.disabled = false;
            }
        });
    }

    // Mobile panel photo buttons
    const btnUploadMobile = document.getElementById('btn-mob-art-upload-photo');
    if (btnUploadMobile) {
        btnUploadMobile.addEventListener('click', () => {
            if (!_currentMobileArticle) return;
            const color = _currentMobileArticle.colors && _currentMobileArticle.colors.length > 0 ? _currentMobileArticle.colors[0] : '';
            requestPhotoUpload({
                onSuccess: async (compressedBlob) => {
                    // Instant local preview
                    const blobUrl = URL.createObjectURL(compressedBlob);
                    const img = document.getElementById('mob-art-photo');
                    const placeholder = document.getElementById('mob-art-photo-placeholder');
                    if (img && placeholder) {
                        img.src = blobUrl;
                        img.style.display = 'block';
                        placeholder.style.display = 'none';
                    }
                    _showToast('Caricamento foto in corso...', 'info');
                    try {
                        const res = await api.uploadCatalogPhoto(_currentMobileArticle.blueprint_id, compressedBlob, color);
                        if (res && res.photo_id) {
                            api.invalidatePhotoCache(res.photo_id);
                            _currentMobileArticle.photo_id = res.photo_id;
                            _updateArticlePhotoPreview('mobile', res.photo_id, true);
                            _showToast('Foto aggiornata con successo!');
                            await _refreshWarehouseView();
                        }
                    } catch (err) {
                        _showToast(err.message || 'Errore durante il caricamento della foto.', 'error');
                        _updateArticlePhotoPreview('mobile', _currentMobileArticle.photo_id, false);
                    }
                },
                onError: (err) => {
                    _showToast('Errore durante la selezione della foto: ' + (err.message || err), 'error');
                }
            });
        });
    }

    const btnGoogleMobile = document.getElementById('btn-mob-art-search-google');
    if (btnGoogleMobile) {
        btnGoogleMobile.addEventListener('click', () => {
            if (!_currentMobileArticle) return;
            const color = _currentMobileArticle.colors && _currentMobileArticle.colors.length > 0 ? _currentMobileArticle.colors[0] : '';
            const q = `${_currentMobileArticle.brand_name || ''} ${_extractBaseArticleName(_currentMobileArticle)} ${color}`.trim();
            const searchUrl = `https://www.google.com/search?tbm=isch&q=${encodeURIComponent(q)}`;
            window.open(searchUrl, '_blank', 'noopener,noreferrer');
        });
    }

    const btnUrlMobile = document.getElementById('btn-mob-art-manual-url');
    if (btnUrlMobile) {
        btnUrlMobile.addEventListener('click', async () => {
            if (!_currentMobileArticle) return;
            const url = prompt('Inserisci l\'URL dell\'immagine da impostare:');
            if (!url || !url.trim()) return;
            const color = _currentMobileArticle.colors && _currentMobileArticle.colors.length > 0 ? _currentMobileArticle.colors[0] : '';
            _showToast('Download foto in corso...', 'info');
            try {
                const res = await api.setCatalogPhotoUrl(_currentMobileArticle.blueprint_id, url.trim(), color);
                if (res && res.photo_id) {
                    api.invalidatePhotoCache(res.photo_id);
                    _currentMobileArticle.photo_id = res.photo_id;
                    _updateArticlePhotoPreview('mobile', res.photo_id, true);
                    _showToast('Foto impostata con successo!');
                    await _refreshWarehouseView();
                }
            } catch (err) {
                _showToast(err.message || 'Impossibile scaricare la foto dall\'URL.', 'error');
            }
        });
    }

    const btnAiMobile = document.getElementById('btn-mob-art-ai-search');
    if (btnAiMobile) {
        btnAiMobile.addEventListener('click', async () => {
            if (!_currentMobileArticle) return;
            const feedback = prompt('Descrivi la foto da cercare su Internet con l\'AI (opzionale):');
            const color = _currentMobileArticle.colors && _currentMobileArticle.colors.length > 0 ? _currentMobileArticle.colors[0] : '';
            _showToast('Ricerca AI su Internet in corso...', 'info');
            btnAiMobile.disabled = true;
            try {
                const res = await api.searchCatalogPhoto(_currentMobileArticle.blueprint_id, color, feedback || null);
                if (res && res.photo_id) {
                    api.invalidatePhotoCache(res.photo_id);
                    _currentMobileArticle.photo_id = res.photo_id;
                    _updateArticlePhotoPreview('mobile', res.photo_id, true);
                    _showToast('Foto trovata e impostata con successo!');
                    await _refreshWarehouseView();
                }
            } catch (err) {
                _showToast(err.message || 'Nessuna foto trovata con l\'AI.', 'error');
            } finally {
                btnAiMobile.disabled = false;
            }
        });
    }
}

function _updateArticlePhotoPreview(target, photoId, forceBust = false) {
    if (!photoId) {
        if (target === 'desktop' || target === 'both') {
            const img = document.getElementById('edit-art-photo');
            const placeholder = document.getElementById('edit-art-photo-placeholder');
            if (img && placeholder) {
                img.src = '';
                img.style.display = 'none';
                placeholder.style.display = 'block';
            }
        }
        if (target === 'mobile' || target === 'both') {
            const img = document.getElementById('mob-art-photo');
            const placeholder = document.getElementById('mob-art-photo-placeholder');
            if (img && placeholder) {
                img.src = '';
                img.style.display = 'none';
                placeholder.style.display = 'block';
            }
        }
        return;
    }

    if (forceBust) {
        api.invalidatePhotoCache(photoId);
    }
    const photoUrl = api.getPhotoUrl(photoId);
    if (target === 'desktop' || target === 'both') {
        const img = document.getElementById('edit-art-photo');
        const placeholder = document.getElementById('edit-art-photo-placeholder');
        if (img && placeholder) {
            img.src = photoUrl;
            img.style.display = 'block';
            placeholder.style.display = 'none';
        }
    }
    if (target === 'mobile' || target === 'both') {
        const img = document.getElementById('mob-art-photo');
        const placeholder = document.getElementById('mob-art-photo-placeholder');
        if (img && placeholder) {
            img.src = photoUrl;
            img.style.display = 'block';
            placeholder.style.display = 'none';
        }
    }
}

let _brandCategoriesCache = {};

async function _loadBrandCategories(brandId) {
    if (!brandId) return {};
    if (_brandCategoriesCache[brandId]) return _brandCategoriesCache[brandId];
    try {
        const res = await api.getBrandCategories(brandId);
        _brandCategoriesCache[brandId] = res.data || res || {};
        return _brandCategoriesCache[brandId];
    } catch (e) {
        console.error("Failed to load brand categories:", e);
        return {};
    }
}

function _resolveBrandId(item) {
    if (item.brand_id) return item.brand_id;
    const brands = window.__LTDB_BRANDS__ || [];
    const b = brands.find(x => x.name.toLowerCase() === (item.brand_name || '').toLowerCase());
    return b ? b.id : null;
}

function _populateCategoryDropdowns(macroSelect, subSelect, hierarchy, currentMacro, currentSub) {
    if (!macroSelect || !subSelect) return;
    const hasHierarchy = hierarchy && Object.keys(hierarchy).length > 0;

    let macroHtml = '<option value="">Macro Categoria...</option>';
    if (hasHierarchy) {
        macroHtml += Object.keys(hierarchy).map(c =>
            `<option value="${_esc(c)}" ${c.toLowerCase() === (currentMacro || '').toLowerCase() ? 'selected' : ''}>${_esc(c)}</option>`
        ).join('');
    } else if (currentMacro) {
        macroHtml += `<option value="${_esc(currentMacro)}" selected>${_esc(currentMacro)}</option>`;
    }
    macroSelect.innerHTML = macroHtml;

    function _updateSubOptions(selectedMacro, selectedSub) {
        let subHtml = '<option value="">Sotto Categoria...</option>';
        if (hasHierarchy && selectedMacro && hierarchy[selectedMacro]) {
            const subs = Object.keys(hierarchy[selectedMacro].sub_categories || {});
            subHtml += subs.map(sc =>
                `<option value="${_esc(sc)}" ${sc.toLowerCase() === (selectedSub || '').toLowerCase() ? 'selected' : ''}>${_esc(sc)}</option>`
            ).join('');
        } else if (selectedSub) {
            subHtml += `<option value="${_esc(selectedSub)}" selected>${_esc(selectedSub)}</option>`;
        }
        subSelect.innerHTML = subHtml;
    }

    _updateSubOptions(macroSelect.value, currentSub);
    macroSelect.onchange = () => {
        _updateSubOptions(macroSelect.value, '');
    };
}

function openEditArticleModal(item) {
    _editingItem = item;
    const modalEdit = document.getElementById('modal-edit-article');
    const inputName = document.getElementById('edit-article-name');
    const inputVendorCode = document.getElementById('edit-article-vendor-code');
    const inputBarcode = document.getElementById('edit-article-barcode');
    const inputDesc = document.getElementById('edit-article-description');
    const inputExtDesc = document.getElementById('edit-article-ext-description');
    const inputTags = document.getElementById('edit-article-tags');
    const inputMaterials = document.getElementById('edit-article-materials');
    const inputQty = document.getElementById('edit-article-qty');
    const macroSelect = document.getElementById('edit-article-macro-cat');
    const subSelect = document.getElementById('edit-article-sub-cat');

    if (!modalEdit) return;

    if (inputName) inputName.value = _extractBaseArticleName(item);
    if (inputVendorCode) inputVendorCode.value = item.vendor_code || '';
    if (inputBarcode) inputBarcode.value = item.barcode || '';
    if (inputDesc) inputDesc.value = item.description || '';
    if (inputExtDesc) inputExtDesc.value = item.extended_description || '';
    if (inputTags) inputTags.value = (item.tags || []).join(', ');
    if (inputMaterials) inputMaterials.value = (item.materials || []).join(', ');
    if (inputQty) inputQty.value = item.stock ?? 0;

    const brandId = _resolveBrandId(item);
    if (macroSelect && subSelect) {
        _populateCategoryDropdowns(macroSelect, subSelect, {}, item.category_name, item.sub_category_name);
        if (brandId) {
            _loadBrandCategories(brandId).then(hierarchy => {
                _populateCategoryDropdowns(macroSelect, subSelect, hierarchy, item.category_name, item.sub_category_name);
            });
        }
    }

    // Photo preview setup
    const photoImg = document.getElementById('edit-art-photo');
    const photoPlaceholder = document.getElementById('edit-art-photo-placeholder');
    const photoWrap = document.getElementById('edit-art-photo-wrap');

    const hasPhoto = Boolean(item.photo_id);
    const photoUrl = hasPhoto ? api.getPhotoUrl(item.photo_id) : '';

    if (photoImg && photoPlaceholder) {
        if (hasPhoto) {
            photoImg.src = photoUrl;
            photoImg.style.display = 'block';
            photoPlaceholder.style.display = 'none';
        } else {
            photoImg.src = '';
            photoImg.style.display = 'none';
            photoPlaceholder.style.display = 'block';
        }
    }
    if (photoWrap) {
        photoWrap.onclick = () => {
            if (_editingItem && _editingItem.photo_id) {
                showImageFullscreen(api.getPhotoUrl(_editingItem.photo_id), _editingItem.article_name);
            }
        };
    }

    modalEdit.classList.add('active');
    setTimeout(() => {
        if (inputName) inputName.focus();
    }, 100);
}

function closeEditArticleModal() {
    const modalEdit = document.getElementById('modal-edit-article');
    const formEdit = document.getElementById('form-edit-article');
    if (modalEdit) modalEdit.classList.remove('active');
    if (formEdit) formEdit.reset();
    _editingItem = null;
}

async function _handleSaveEditArticle(e) {
    e.preventDefault();
    if (!_editingItem) return;

    const inputName = document.getElementById('edit-article-name');
    const inputVendorCode = document.getElementById('edit-article-vendor-code');
    const inputBarcode = document.getElementById('edit-article-barcode');
    const inputDesc = document.getElementById('edit-article-description');
    const inputExtDesc = document.getElementById('edit-article-ext-description');
    const inputTags = document.getElementById('edit-article-tags');
    const inputMaterials = document.getElementById('edit-article-materials');
    const inputQty = document.getElementById('edit-article-qty');
    const macroSelect = document.getElementById('edit-article-macro-cat');
    const subSelect = document.getElementById('edit-article-sub-cat');
    const btnSave = document.getElementById('btn-save-edit-article');

    const articleName = inputName ? inputName.value.trim() : '';
    const vendorCode = inputVendorCode ? inputVendorCode.value.trim() : '';
    const barcode = inputBarcode ? inputBarcode.value.trim() : '';
    const description = inputDesc ? inputDesc.value.trim() : '';
    const extendedDescription = inputExtDesc ? inputExtDesc.value.trim() : '';
    const tags = (inputTags ? inputTags.value : '')
        .split(',')
        .map(s => s.trim().toLowerCase())
        .filter(Boolean);
    const materials = (inputMaterials ? inputMaterials.value : '')
        .split(',')
        .map(s => s.trim().toLowerCase())
        .filter(Boolean);

    const rawQty = inputQty ? parseInt(inputQty.value, 10) : 0;
    const newQty = isNaN(rawQty) ? 0 : Math.max(0, rawQty);
    const newMacroCat = macroSelect ? macroSelect.value.trim() : '';
    const newSubCat = subSelect ? subSelect.value.trim() : '';

    if (!articleName) {
        _showToast('Il nome dell\'articolo non può essere vuoto.', 'error');
        return;
    }
    if (!description) {
        _showToast('La descrizione breve non può essere vuota.', 'error');
        return;
    }
    if (tags.length === 0) {
        _showToast('Inserisci almeno un tag.', 'error');
        return;
    }
    if (materials.length === 0) {
        _showToast('Inserisci almeno un materiale.', 'error');
        return;
    }

    const color = _editingItem.colors && _editingItem.colors.length > 0 ? _editingItem.colors[0] : null;
    const payload = {
        article_name: articleName,
        description: description,
        extended_description: extendedDescription,
        tags: tags,
        materials: materials,
        vendor_code: vendorCode,
        barcode: barcode,
        color: color,
        quantity: newQty,
        category_name: newMacroCat,
        sub_category_name: newSubCat
    };

    if (btnSave) {
        btnSave.disabled = true;
        btnSave.textContent = 'Salvataggio...';
    }

    try {
        await api.patchCatalogItem(_editingItem.blueprint_id, payload);
        closeEditArticleModal();

        const detailOverlay = document.getElementById('article-fullscreen-modal');
        if (detailOverlay) detailOverlay.style.display = 'none';

        await _refreshWarehouseView();
    } catch (err) {
        _showToast(err.message || 'Errore durante l\'aggiornamento.', 'error');
    } finally {
        if (btnSave) {
            btnSave.disabled = false;
            btnSave.textContent = 'Salva Modifiche';
        }
    }
}

function openDeleteArticleModal(item) {
    _deletingItem = item;
    const modalDelete = document.getElementById('modal-delete-article');
    const deleteMsg = document.getElementById('delete-article-msg');
    if (!modalDelete) return;

    if (deleteMsg) {
        deleteMsg.textContent = `Sei sicuro di voler eliminare l'articolo "${item.article_name}" dal magazzino?`;
    }
    modalDelete.classList.add('active');
}

function closeDeleteArticleModal() {
    const modalDelete = document.getElementById('modal-delete-article');
    if (modalDelete) modalDelete.classList.remove('active');
    _deletingItem = null;
}

async function _handleConfirmDeleteArticle() {
    if (!_deletingItem) return;
    const btnConfirm = document.getElementById('btn-confirm-delete-article');
    const color = _deletingItem.colors && _deletingItem.colors.length > 0 ? _deletingItem.colors[0] : null;

    if (btnConfirm) {
        btnConfirm.disabled = true;
        btnConfirm.textContent = 'Eliminazione...';
    }

    try {
        await api.deleteCatalogItem(_deletingItem.blueprint_id, color);
        _showToast('Articolo eliminato con successo.');
        closeDeleteArticleModal();

        const detailOverlay = document.getElementById('article-fullscreen-modal');
        if (detailOverlay) detailOverlay.style.display = 'none';

        closeArticleMobilePanel();

        await _refreshWarehouseView();
    } catch (err) {
        _showToast(err.message || 'Errore durante l\'eliminazione.', 'error');
    } finally {
        if (btnConfirm) {
            btnConfirm.disabled = false;
            btnConfirm.textContent = 'Elimina';
        }
    }
}

// ============================================================================
// Mobile Article Panel Logic
// ============================================================================

export function openArticleMobilePanel(item) {
    _currentMobileArticle = item;
    const container = document.getElementById('magazzino-container');
    const panel = document.getElementById('article-mobile-panel');
    if (!container || !panel) return;

    const photoImg = document.getElementById('mob-art-photo');
    const photoPlaceholder = document.getElementById('mob-art-photo-placeholder');
    const brandEl = document.getElementById('mob-art-brand');
    const catEl = document.getElementById('mob-art-category');
    const stockEl = document.getElementById('mob-art-stock');

    const nameInput = document.getElementById('mob-art-name');
    const vendorInput = document.getElementById('mob-art-vendor-code');
    const barcodeInput = document.getElementById('mob-art-barcode');
    const descInput = document.getElementById('mob-art-description');
    const extDescInput = document.getElementById('mob-art-ext-description');
    const tagsInput = document.getElementById('mob-art-tags');
    const materialsInput = document.getElementById('mob-art-materials');

    const hasPhoto = Boolean(item.photo_id);
    const photoUrl = hasPhoto ? api.getPhotoUrl(item.photo_id) : '';

    if (photoImg && photoPlaceholder) {
        if (hasPhoto) {
            photoImg.src = photoUrl;
            photoImg.style.display = 'block';
            photoPlaceholder.style.display = 'none';
        } else {
            photoImg.src = '';
            photoImg.style.display = 'none';
            photoPlaceholder.style.display = 'block';
        }
    }

    if (brandEl) brandEl.textContent = item.brand_name || '';
    if (catEl) {
        if (item.category_name && item.sub_category_name) {
            catEl.textContent = `${item.category_name} ↳ ${item.sub_category_name}`;
        } else {
            catEl.textContent = item.category_name || item.sub_category_name || '-';
        }
    }
    if (stockEl) {
        const stockLabel = item.stock > 0 ? `Disponibile (${item.stock})` : 'Esaurito';
        const stockClass = item.stock > 5 ? 'available' : (item.stock > 0 ? 'low' : 'zero');
        stockEl.className = `stock-badge stock-badge--${stockClass}`;
        stockEl.textContent = stockLabel;
    }

    if (nameInput) nameInput.value = _extractBaseArticleName(item);
    if (vendorInput) vendorInput.value = item.vendor_code || '';
    if (barcodeInput) barcodeInput.value = item.barcode || '';
    if (descInput) descInput.value = item.description || '';
    if (extDescInput) extDescInput.value = item.extended_description || '';
    if (tagsInput) tagsInput.value = (item.tags || []).join(', ');
    if (materialsInput) materialsInput.value = (item.materials || []).join(', ');

    const inputQtyMobile = document.getElementById('mob-art-qty');
    if (inputQtyMobile) inputQtyMobile.value = item.stock ?? 0;

    const macroSelectMobile = document.getElementById('mob-art-macro-cat');
    const subSelectMobile = document.getElementById('mob-art-sub-cat');
    const brandIdMobile = _resolveBrandId(item);
    if (macroSelectMobile && subSelectMobile) {
        _populateCategoryDropdowns(macroSelectMobile, subSelectMobile, {}, item.category_name, item.sub_category_name);
        if (brandIdMobile) {
            _loadBrandCategories(brandIdMobile).then(hierarchy => {
                _populateCategoryDropdowns(macroSelectMobile, subSelectMobile, hierarchy, item.category_name, item.sub_category_name);
            });
        }
    }

    container.classList.add('mobile-panel-open');
}

export function closeArticleMobilePanel() {
    const container = document.getElementById('magazzino-container');
    if (container) container.classList.remove('mobile-panel-open');
    _currentMobileArticle = null;
}

async function _handleSaveArticleMobile(e) {
    e.preventDefault();
    if (!_currentMobileArticle) return;

    const nameInput = document.getElementById('mob-art-name');
    const vendorInput = document.getElementById('mob-art-vendor-code');
    const barcodeInput = document.getElementById('mob-art-barcode');
    const descInput = document.getElementById('mob-art-description');
    const extDescInput = document.getElementById('mob-art-ext-description');
    const tagsInput = document.getElementById('mob-art-tags');
    const materialsInput = document.getElementById('mob-art-materials');
    const inputQtyMobile = document.getElementById('mob-art-qty');
    const macroSelectMobile = document.getElementById('mob-art-macro-cat');
    const subSelectMobile = document.getElementById('mob-art-sub-cat');
    const btnSave = document.getElementById('btn-save-article-mobile');

    const articleName = nameInput ? nameInput.value.trim() : '';
    const vendorCode = vendorInput ? vendorInput.value.trim() : '';
    const barcode = barcodeInput ? barcodeInput.value.trim() : '';
    const description = descInput ? descInput.value.trim() : '';
    const extendedDescription = extDescInput ? extDescInput.value.trim() : '';
    const tags = (tagsInput ? tagsInput.value : '')
        .split(',')
        .map(s => s.trim().toLowerCase())
        .filter(Boolean);
    const materials = (materialsInput ? materialsInput.value : '')
        .split(',')
        .map(s => s.trim().toLowerCase())
        .filter(Boolean);

    const rawQty = inputQtyMobile ? parseInt(inputQtyMobile.value, 10) : 0;
    const newQty = isNaN(rawQty) ? 0 : Math.max(0, rawQty);
    const newMacroCat = macroSelectMobile ? macroSelectMobile.value.trim() : '';
    const newSubCat = subSelectMobile ? subSelectMobile.value.trim() : '';

    if (!articleName) {
        _showToast('Il nome dell\'articolo non può essere vuoto.', 'error');
        return;
    }
    if (!description) {
        _showToast('La descrizione breve non può essere vuota.', 'error');
        return;
    }
    if (tags.length === 0) {
        _showToast('Inserisci almeno un tag.', 'error');
        return;
    }
    if (materials.length === 0) {
        _showToast('Inserisci almeno un materiale.', 'error');
        return;
    }

    const color = _currentMobileArticle.colors && _currentMobileArticle.colors.length > 0 ? _currentMobileArticle.colors[0] : null;
    const payload = {
        article_name: articleName,
        description: description,
        extended_description: extendedDescription,
        tags: tags,
        materials: materials,
        vendor_code: vendorCode,
        barcode: barcode,
        color: color,
        quantity: newQty,
        category_name: newMacroCat,
        sub_category_name: newSubCat
    };

    if (btnSave) {
        btnSave.disabled = true;
        btnSave.textContent = 'Salvataggio...';
    }

    try {
        await api.patchCatalogItem(_currentMobileArticle.blueprint_id, payload);
        closeArticleMobilePanel();
        await _refreshWarehouseView();
    } catch (err) {
        _showToast(err.message || 'Errore durante l\'aggiornamento.', 'error');
    } finally {
        if (btnSave) {
            btnSave.disabled = false;
            btnSave.textContent = 'Salva Modifiche';
        }
    }
}

// ============================================================================
// Fullscreen Image Lightbox & Fullscreen Article Detail View
// ============================================================================

function showImageFullscreen(imageUrl, altText = '') {
    let imgOverlay = document.getElementById('image-fullscreen-modal');
    if (!imgOverlay) {
        imgOverlay = document.createElement('div');
        imgOverlay.id = 'image-fullscreen-modal';
        imgOverlay.style.position = 'fixed';
        imgOverlay.style.top = '0';
        imgOverlay.style.left = '0';
        imgOverlay.style.width = '100vw';
        imgOverlay.style.height = '100vh';
        imgOverlay.style.backgroundColor = 'rgba(0, 0, 0, 0.92)';
        imgOverlay.style.zIndex = '12000';
        imgOverlay.style.display = 'none';
        imgOverlay.style.justifyContent = 'center';
        imgOverlay.style.alignItems = 'center';
        imgOverlay.style.backdropFilter = 'blur(6px)';
        imgOverlay.style.cursor = 'zoom-out';
        document.body.appendChild(imgOverlay);

        imgOverlay.addEventListener('click', (e) => {
            if (e.target === imgOverlay || e.target.closest('.close-image-fullscreen')) {
                imgOverlay.style.display = 'none';
            }
        });

        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && imgOverlay.style.display === 'flex') {
                imgOverlay.style.display = 'none';
            }
        });
    }

    imgOverlay.innerHTML = `
        <button type="button" class="close-image-fullscreen" title="Chiudi immagine"
                style="position: absolute; top: 20px; right: 24px; background: var(--bg-elevated); border: 1px solid var(--border-color); color: var(--text-primary); width: 42px; height: 42px; border-radius: 50%; cursor: pointer; display: flex; align-items: center; justify-content: center; box-shadow: 0 4px 12px rgba(0,0,0,0.4);">
            <span class="material-symbols-rounded" style="font-size: 24px;">close</span>
        </button>
        <img src="${imageUrl}" alt=""
             style="max-width: 92vw; max-height: 90vh; object-fit: contain; border-radius: 10px; box-shadow: 0 12px 40px rgba(0,0,0,0.65); cursor: default;"
             onclick="event.stopPropagation()">
    `;
    imgOverlay.style.display = 'flex';
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

    const hasPhoto = Boolean(item.photo_id);
    const photoUrl = hasPhoto ? api.getPhotoUrl(item.photo_id) : '';
    const photoHtml = hasPhoto
        ? `<div class="fullscreen-article-photo" style="width:100%; height:100%; display:flex; justify-content:center; align-items:center; overflow:hidden; border-radius:10px; background:var(--bg-app); cursor:zoom-in;">
               <img src="${photoUrl}" style="width:100%; height:100%; max-height:68vh; object-fit:contain; border-radius:10px;" alt="" onerror="this.style.display='none'">
           </div>`
        : `<div style="width:100%; height:320px; display:flex; align-items:center; justify-content:center; font-size:48px; background:var(--bg-elevated); border-radius:10px;">🖼</div>`;

    const tags = (item.tags || []).map(t => `<span class="chip">${_esc(t)}</span>`).join('');
    const materials = (item.materials || []).map(m => `<span class="mini-chip">${_esc(m)}</span>`).join('');

    const stockLabel = item.stock > 0 ? `Disponibile (${item.stock})` : 'Esaurito';
    const stockClass = item.stock > 5 ? 'available' : (item.stock > 0 ? 'low' : 'zero');

    overlay.innerHTML = `
        <div style="background: var(--bg-surface); width: 94%; max-width: 1040px; max-height: 92vh; overflow-y: auto; border-radius: 16px; position: relative; display: flex; flex-direction: column; box-shadow: 0 16px 48px rgba(0,0,0,0.6); border: 1px solid var(--border-color);">
            <div style="display: flex; justify-content: space-between; align-items: center; padding: 18px 24px; border-bottom: 1px solid var(--border-color);">
                <h2 style="margin: 0; font-size: 18px; font-weight: 600;">Dettaglio Articolo</h2>
                <div style="display: flex; align-items: center; gap: 8px;">
                    <button type="button" class="close-article-modal action-btn" title="Chiudi">
                        <span class="material-symbols-rounded" style="font-size: 24px;">close</span>
                    </button>
                </div>
            </div>
            <div style="display: flex; flex-direction: row; gap: 30px; padding: 28px; flex-wrap: wrap;">
                <div style="flex: 1; text-align: center; min-width: 260px; max-width: 420px; margin: 0 auto;">
                    ${photoHtml}
                </div>
                <div style="flex: 1.2; display: flex; flex-direction: column; gap: 16px; min-width: 300px;">
                    <div>
                        <div style="font-size: 14px; color: var(--accent); font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 4px;">${_esc(item.brand_name)}</div>
                        <h1 style="margin: 0; font-size: 24px; color: var(--text-primary); line-height: 1.25;">${_esc(item.article_name)}</h1>
                    </div>

                    ${item.description ? `<div style="font-size: 14px; color: var(--text-primary);">${_esc(item.description)}</div>` : ''}
                    
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 14px; background: var(--bg-app); padding: 16px; border-radius: 10px; margin-top: 4px;">
                        <div>
                            <div style="font-size: 12px; color: var(--text-muted);">Categoria</div>
                            <div style="font-weight: 500; font-size: 14px;">${item.category_name ? _esc(item.category_name) : 'N/D'}</div>
                        </div>
                        <div>
                            <div style="font-size: 12px; color: var(--text-muted);">Sotto-categoria</div>
                            <div style="font-weight: 500; font-size: 14px;">${item.sub_category_name ? _esc(item.sub_category_name) : 'N/D'}</div>
                        </div>
                        <div>
                            <div style="font-size: 12px; color: var(--text-muted);">Barcode (EAN)</div>
                            <div style="font-weight: 500; font-size: 14px; font-family: monospace;">${item.barcode ? _esc(item.barcode) : 'N/D'}</div>
                        </div>
                        <div>
                            <div style="font-size: 12px; color: var(--text-muted);">Codice Fornitore</div>
                            <div style="font-weight: 500; font-size: 14px; font-family: monospace;">${item.vendor_code ? _esc(item.vendor_code) : 'N/D'}</div>
                        </div>
                        <div>
                            <div style="font-size: 12px; color: var(--text-muted);">Materiali</div>
                            <div style="display: flex; gap: 4px; flex-wrap: wrap; margin-top: 2px;">${materials || '<span style="font-size: 13px;">N/D</span>'}</div>
                        </div>
                        <div>
                            <div style="font-size: 12px; color: var(--text-muted);">Giacenza</div>
                            <div style="margin-top: 2px;"><span class="stock-badge stock-badge--${stockClass}">${stockLabel}</span></div>
                        </div>
                    </div>

                    ${item.extended_description ? `<div style="font-size: 14px; color: var(--text-secondary); white-space: pre-wrap; line-height: 1.5;">${_esc(item.extended_description)}</div>` : ''}

                    ${tags ? `<div style="display: flex; gap: 8px; flex-wrap: wrap;">${tags}</div>` : ''}
                </div>
            </div>
        </div>
    `;

    const fullPhotoEl = overlay.querySelector('.fullscreen-article-photo');
    if (fullPhotoEl && hasPhoto) {
        fullPhotoEl.addEventListener('click', (e) => {
            e.stopPropagation();
            showImageFullscreen(photoUrl, item.article_name);
        });
    }

    overlay.style.display = 'flex';
}
