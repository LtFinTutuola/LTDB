/**
 * brands_page.js
 * Client-side CRUD logic for Brand management in LTDB.
 */
import * as api from './api.js';

function _esc(str) {
    if (typeof str !== 'string') return '';
    return str.replace(/[&<>"']/g, m => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;',
        '"': '&quot;', "'": '&#39;'
    })[m]);
}

function showToast(message, type = 'success') {
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
    toast.style.zIndex = '9999';
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

// State
let allBrands = [];
let allMacroCategories = [];
let editingBrandId = null;
let deletingBrand = null;

// DOM references
const dom = {
    tbody: document.getElementById('brands-tbody'),
    searchInput: document.getElementById('search-brands'),
    statsCount: document.getElementById('brands-count'),
    btnNew: document.getElementById('btn-new-brand'),
    btnNewMobile: document.getElementById('btn-new-brand-mobile'),
    loading: document.getElementById('loading-indicator'),
    empty: document.getElementById('empty-state'),

    // Mobile Dedicated Panel
    mobilePanel: document.getElementById('brand-mobile-panel'),
    btnPanelBack: document.getElementById('btn-brand-panel-back'),
    panelBrandTitle: document.getElementById('brand-panel-title'),
    btnPanelDelete: document.getElementById('btn-brand-panel-delete'),
    formBrandMobile: document.getElementById('form-brand-mobile'),
    mobInputBrandName: document.getElementById('mob-brand-name'),
    mobCategoriesList: document.getElementById('mob-brand-categories-list'),
    
    // Modal Brand Form
    modalBrand: document.getElementById('modal-brand'),
    modalBrandTitle: document.getElementById('modal-brand-title'),
    formBrand: document.getElementById('form-brand'),
    inputBrandName: document.getElementById('brand-name'),
    categoriesList: document.getElementById('brand-categories-list'),
    btnCancelBrand: document.getElementById('btn-cancel-brand'),
    btnCloseBrand: document.getElementById('btn-close-brand'),
    
    // Modal Delete
    modalDelete: document.getElementById('modal-delete'),
    deleteMsg: document.getElementById('delete-msg'),
    deleteWarning: document.getElementById('delete-warning'),
    btnConfirmDelete: document.getElementById('btn-confirm-delete'),
    btnCancelDelete: document.getElementById('btn-cancel-delete'),
    btnCloseDelete: document.getElementById('btn-close-delete'),
};

async function loadData() {
    dom.loading.style.display = 'flex';
    dom.empty.style.display = 'none';
    dom.tbody.innerHTML = '';

    try {
        const [brands, categories] = await Promise.all([
            api.getBrandsDetailed(),
            api.getCategoriesDetailed(),
        ]);
        allBrands = brands.slice().sort((a, b) => a.name.localeCompare(b.name, 'it', { sensitivity: 'base' }));
        // Only top-level / macro categories (parent_id is null) are associated with brands
        allMacroCategories = categories
            .filter(c => !c.parent_id)
            .sort((a, b) => a.name.localeCompare(b.name, 'it', { sensitivity: 'base' }));
        renderTable();
    } catch (err) {
        showToast(`Errore durante il caricamento: ${err.message}`, 'error');
    } finally {
        dom.loading.style.display = 'none';
    }
}

function renderTable() {
    const query = dom.searchInput.value.trim().toLowerCase();
    const filtered = allBrands
        .filter(b => b.name.toLowerCase().includes(query))
        .sort((a, b) => a.name.localeCompare(b.name, 'it', { sensitivity: 'base' }));


    dom.statsCount.textContent = `${filtered.length} brand`;

    if (filtered.length === 0) {
        dom.tbody.innerHTML = '';
        dom.empty.style.display = 'flex';
        return;
    }

    dom.empty.style.display = 'none';
    dom.tbody.innerHTML = filtered.map(b => {
        let catBadges = '<span style="color: var(--text-muted); font-style: italic;">Nessuna</span>';
        if (b.category_names && b.category_names.length > 0) {
            catBadges = '<div class="tags-list">' +
                b.category_names.slice(0, 5).map(catName => `<span class="mini-chip">${_esc(catName)}</span>`).join('') +
                (b.category_names.length > 5 ? `<span class="mini-chip mini-chip--more">+${b.category_names.length - 5}</span>` : '') +
                '</div>';
        }

        return `
            <tr data-id="${_esc(b.id)}">
                <td>
                    <span class="brand-name-text" style="font-weight: 600; font-size: 14px;">${_esc(b.name)}</span>
                </td>
                <td>${catBadges}</td>
                <td>
                    <span class="count-badge">${b.blueprint_count}</span>
                </td>
                <td>
                    <div class="action-buttons">
                        <button class="action-btn btn-edit-brand" data-id="${_esc(b.id)}" title="Modifica Brand">
                            <span class="material-symbols-rounded" style="font-size: 18px;">edit</span>
                        </button>
                        <button class="action-btn action-btn--delete btn-delete-brand" data-id="${_esc(b.id)}" title="Elimina Brand">
                            <span class="material-symbols-rounded" style="font-size: 18px;">delete</span>
                        </button>
                    </div>
                </td>
            </tr>
        `;
    }).join('');
}

function openBrandModal(brand = null) {
    editingBrandId = brand ? brand.id : null;
    dom.modalBrandTitle.textContent = brand ? 'Modifica Brand' : 'Nuovo Brand';
    dom.inputBrandName.value = brand ? brand.name : '';

    const selectedIds = new Set(brand && brand.category_ids ? brand.category_ids : []);

    dom.categoriesList.innerHTML = allMacroCategories.map(cat => {
        const isChecked = selectedIds.has(cat.id) ? 'checked' : '';
        return `
            <label class="checkbox-item">
                <input type="checkbox" value="${_esc(cat.id)}" ${isChecked}>
                <span>${_esc(cat.name)}</span>
            </label>
        `;
    }).join('');

    dom.modalBrand.classList.add('active');
    setTimeout(() => dom.inputBrandName.focus(), 100);
}

function closeBrandModal() {
    dom.modalBrand.classList.remove('active');
    editingBrandId = null;
    dom.formBrand.reset();
}

async function handleSaveBrand(e) {
    e.preventDefault();
    const name = dom.inputBrandName.value.trim();
    if (!name) {
        showToast('Inserisci il nome del brand.', 'error');
        return;
    }

    const checkedCats = Array.from(
        dom.categoriesList.querySelectorAll('input[type="checkbox"]:checked')
    ).map(cb => cb.value);

    try {
        if (editingBrandId) {
            await api.updateBrand(editingBrandId, { name, category_ids: checkedCats });
        } else {
            await api.createBrand({ name, category_ids: checkedCats });
            showToast('Nuovo brand creato con successo.');
        }
        closeBrandModal();
        await loadData();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

function openDeleteModal(brand) {
    deletingBrand = brand;
    dom.deleteMsg.textContent = `Sei sicuro di voler eliminare il brand "${brand.name}"?`;

    if (brand.blueprint_count > 0) {
        dom.deleteWarning.style.display = 'block';
        dom.deleteWarning.textContent = `⚠️ Impossibile eliminare: ci sono ${brand.blueprint_count} articoli associati a questo brand.`;
        dom.btnConfirmDelete.disabled = true;
        dom.btnConfirmDelete.style.opacity = '0.5';
        dom.btnConfirmDelete.style.cursor = 'not-allowed';
    } else {
        dom.deleteWarning.style.display = 'none';
        dom.btnConfirmDelete.disabled = false;
        dom.btnConfirmDelete.style.opacity = '1';
        dom.btnConfirmDelete.style.cursor = 'pointer';
    }

    dom.modalDelete.classList.add('active');
}

function closeDeleteModal() {
    dom.modalDelete.classList.remove('active');
    deletingBrand = null;
}

async function handleConfirmDelete() {
    if (!deletingBrand) return;
    try {
        await api.deleteBrand(deletingBrand.id);
        showToast('Brand eliminato con successo.');
        closeDeleteModal();
        closeBrandMobilePanel();
        await loadData();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

// Mobile Panel Logic
let currentMobileBrand = null;

function openBrandMobilePanel(brand = null) {
    currentMobileBrand = brand;
    editingBrandId = brand ? brand.id : null;

    if (dom.panelBrandTitle) {
        dom.panelBrandTitle.textContent = brand ? 'Modifica Brand' : 'Aggiungi Brand';
    }
    if (dom.btnPanelDelete) {
        dom.btnPanelDelete.style.visibility = brand ? 'visible' : 'hidden';
        dom.btnPanelDelete.style.display = brand ? 'inline-flex' : 'none';
    }

    const saveBtn = document.getElementById('btn-save-brand-mobile');
    if (saveBtn) {
        saveBtn.innerHTML = brand
            ? '<span class="material-symbols-rounded">check</span> Salva Modifiche'
            : '<span class="material-symbols-rounded">add</span> Aggiungi Brand';
    }

    if (dom.mobInputBrandName) dom.mobInputBrandName.value = brand ? brand.name : '';

    const selectedIds = new Set(brand && brand.category_ids ? brand.category_ids : []);
    if (dom.mobCategoriesList) {
        dom.mobCategoriesList.innerHTML = allMacroCategories.map(cat => {
            const isChecked = selectedIds.has(cat.id) ? 'checked' : '';
            return `
                <label class="checkbox-item">
                    <input type="checkbox" value="${_esc(cat.id)}" ${isChecked}>
                    <span>${_esc(cat.name)}</span>
                </label>
            `;
        }).join('');
    }

    const page = document.querySelector('.pim-page');
    if (page) page.classList.add('mobile-panel-open');
    if (dom.mobInputBrandName) setTimeout(() => dom.mobInputBrandName.focus(), 150);
}

function closeBrandMobilePanel() {
    const page = document.querySelector('.pim-page');
    if (page) page.classList.remove('mobile-panel-open');
    currentMobileBrand = null;
    editingBrandId = null;
    if (dom.formBrandMobile) dom.formBrandMobile.reset();
}

async function handleSaveBrandMobile(e) {
    e.preventDefault();
    const name = dom.mobInputBrandName ? dom.mobInputBrandName.value.trim() : '';
    if (!name) {
        showToast('Inserisci il nome del brand.', 'error');
        return;
    }

    const checkedCats = !dom.mobCategoriesList ? [] : Array.from(
        dom.mobCategoriesList.querySelectorAll('input[type="checkbox"]:checked')
    ).map(cb => cb.value);

    try {
        if (editingBrandId) {
            await api.updateBrand(editingBrandId, { name, category_ids: checkedCats });
        } else {
            await api.createBrand({ name, category_ids: checkedCats });
            showToast('Nuovo brand creato con successo.');
        }
        closeBrandMobilePanel();
        await loadData();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

// Event Listeners
document.addEventListener('DOMContentLoaded', () => {
    loadData();

    dom.searchInput.addEventListener('input', renderTable);

    dom.btnNew.addEventListener('click', () => openBrandModal(null));
    if (dom.btnNewMobile) {
        dom.btnNewMobile.addEventListener('click', () => openBrandMobilePanel(null));
    }
    dom.btnCancelBrand.addEventListener('click', closeBrandModal);
    dom.btnCloseBrand.addEventListener('click', closeBrandModal);
    dom.formBrand.addEventListener('submit', handleSaveBrand);

    // Mobile Panel listeners
    if (dom.btnPanelBack) {
        dom.btnPanelBack.addEventListener('click', closeBrandMobilePanel);
    }
    if (dom.btnPanelDelete) {
        dom.btnPanelDelete.addEventListener('click', () => {
            if (currentMobileBrand) {
                openDeleteModal(currentMobileBrand);
            }
        });
    }
    if (dom.formBrandMobile) {
        dom.formBrandMobile.addEventListener('submit', handleSaveBrandMobile);
    }

    dom.btnCancelDelete.addEventListener('click', closeDeleteModal);
    dom.btnCloseDelete.addEventListener('click', closeDeleteModal);
    dom.btnConfirmDelete.addEventListener('click', handleConfirmDelete);

    dom.tbody.addEventListener('click', (e) => {
        const editBtn = e.target.closest('.btn-edit-brand');
        if (editBtn) {
            const id = editBtn.dataset.id;
            const b = allBrands.find(x => x.id === id);
            if (b) openBrandModal(b);
            return;
        }

        const deleteBtn = e.target.closest('.btn-delete-brand');
        if (deleteBtn) {
            const id = deleteBtn.dataset.id;
            const b = allBrands.find(x => x.id === id);
            if (b) openDeleteModal(b);
            return;
        }

        // On mobile view: tapping anywhere on the brand row opens dedicated edit panel
        if (window.innerWidth <= 768) {
            const row = e.target.closest('tr');
            if (row && row.dataset.id) {
                const b = allBrands.find(x => x.id === row.dataset.id);
                if (b) {
                    openBrandMobilePanel(b);
                }
            }
        }
    });

    // Close on backdrop click
    dom.modalBrand.addEventListener('click', (e) => {
        if (e.target === dom.modalBrand) closeBrandModal();
    });
    dom.modalDelete.addEventListener('click', (e) => {
        if (e.target === dom.modalDelete) closeDeleteModal();
    });
});
