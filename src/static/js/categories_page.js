/**
 * categories_page.js
 * Client-side hierarchical CRUD logic for Categories management in LTDB.
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
let allCategories = [];
let allBrands = [];
let editingCategoryId = null;
let deletingCategory = null;

// DOM references
const dom = {
    tbody: document.getElementById('categories-tbody'),
    searchInput: document.getElementById('search-categories'),
    filterType: document.getElementById('filter-category-type'),
    statsCount: document.getElementById('categories-count'),
    btnNew: document.getElementById('btn-new-category'),
    btnNewMobile: document.getElementById('btn-new-category-mobile'),
    loading: document.getElementById('loading-indicator'),
    empty: document.getElementById('empty-state'),

    // Mobile Dedicated Panel
    mobilePanel: document.getElementById('category-mobile-panel'),
    btnPanelBack: document.getElementById('btn-category-panel-back'),
    panelCategoryTitle: document.getElementById('category-panel-title'),
    btnPanelDelete: document.getElementById('btn-category-panel-delete'),
    formCategoryMobile: document.getElementById('form-category-mobile'),
    mobInputName: document.getElementById('mob-cat-name'),
    mobInputDesc: document.getElementById('mob-cat-description'),
    mobSelectParent: document.getElementById('mob-cat-parent'),
    mobSectionBrands: document.getElementById('mob-section-brands'),
    mobBrandsList: document.getElementById('mob-cat-brands-list'),
    mobSubInheritHint: document.getElementById('mob-sub-inherit-hint'),

    // Modal Category Form
    modalCategory: document.getElementById('modal-category'),
    modalCategoryTitle: document.getElementById('modal-category-title'),
    formCategory: document.getElementById('form-category'),
    inputName: document.getElementById('cat-name'),
    inputDesc: document.getElementById('cat-description'),
    selectParent: document.getElementById('cat-parent'),
    sectionBrands: document.getElementById('section-brands-assignment'),
    brandsList: document.getElementById('cat-brands-list'),
    subInheritHint: document.getElementById('sub-inherit-hint'),
    btnCancelCategory: document.getElementById('btn-cancel-category'),
    btnCloseCategory: document.getElementById('btn-close-category'),

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
        const [categories, brands] = await Promise.all([
            api.getCategoriesDetailed(),
            api.getBrandsDetailed(),
        ]);
        allCategories = categories.slice().sort((a, b) => a.name.localeCompare(b.name, 'it', { sensitivity: 'base' }));
        allBrands = brands.slice().sort((a, b) => a.name.localeCompare(b.name, 'it', { sensitivity: 'base' }));
        renderTable();
    } catch (err) {
        showToast(`Errore durante il caricamento: ${err.message}`, 'error');
    } finally {
        dom.loading.style.display = 'none';
    }
}

function renderTable() {
    const query = dom.searchInput.value.trim().toLowerCase();
    const typeFilter = dom.filterType.value; // 'all', 'macro', 'sub'

    // Separate into macro categories and subcategories
    const macroCats = allCategories
        .filter(c => !c.parent_id)
        .sort((a, b) => a.name.localeCompare(b.name, 'it', { sensitivity: 'base' }));
    const subCatsByParent = new Map();


    allCategories.filter(c => !!c.parent_id).forEach(sub => {
        if (!subCatsByParent.has(sub.parent_id)) {
            subCatsByParent.set(sub.parent_id, []);
        }
        subCatsByParent.get(sub.parent_id).push(sub);
    });

    let renderedRows = [];
    let visibleCount = 0;

    macroCats.forEach(macro => {
        const subs = subCatsByParent.get(macro.id) || [];
        const macroMatches = macro.name.toLowerCase().includes(query) || (macro.description && macro.description.toLowerCase().includes(query));
        const matchedSubs = subs.filter(s => s.name.toLowerCase().includes(query) || (s.description && s.description.toLowerCase().includes(query)));

        const showMacro = (typeFilter === 'all' || typeFilter === 'macro') && (macroMatches || matchedSubs.length > 0);

        if (showMacro) {
            visibleCount++;
            let brandBadges = '<span style="color: var(--text-muted); font-style: italic;">Nessuno</span>';
            if (macro.brand_names && macro.brand_names.length > 0) {
                brandBadges = '<div class="tags-list">' +
                    macro.brand_names.slice(0, 4).map(bName => `<span class="mini-chip">${_esc(bName)}</span>`).join('') +
                    (macro.brand_names.length > 4 ? `<span class="mini-chip mini-chip--more">+${macro.brand_names.length - 4}</span>` : '') +
                    '</div>';
            }

            renderedRows.push(`
                <tr class="macro-row" data-id="${_esc(macro.id)}">
                    <td>
                        <div class="cat-cell-left" style="display: flex; align-items: center; gap: 8px;">
                            <span class="material-symbols-rounded cat-type-icon cat-type-icon--macro" style="color: var(--accent); font-size: 20px;">folder</span>
                            <span class="cat-name-text" style="font-weight: 600; font-size: 14px;">${_esc(macro.name)}</span>
                            <span class="badge-category badge-category--macro">Macro</span>
                        </div>
                    </td>
                    <td>
                        <span class="cat-desc-text" style="color: var(--text-secondary); font-size: 12px; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;">
                            ${_esc(macro.description || '')}
                        </span>
                    </td>
                    <td>${brandBadges}</td>
                    <td>
                        <span class="count-badge">${macro.subcategories_count}</span>
                    </td>
                    <td>
                        <span class="count-badge">${macro.blueprint_count}</span>
                    </td>
                    <td>
                        <div class="action-buttons">
                            <button class="action-btn action-btn--add btn-add-sub" data-parent-id="${_esc(macro.id)}" title="Aggiungi Sottocategoria">
                                <span class="material-symbols-rounded" style="font-size: 18px;">add_circle</span>
                            </button>
                            <button class="action-btn btn-edit-category" data-id="${_esc(macro.id)}" title="Modifica Categoria">
                                <span class="material-symbols-rounded" style="font-size: 18px;">edit</span>
                            </button>
                            <button class="action-btn action-btn--delete btn-delete-category" data-id="${_esc(macro.id)}" title="Elimina Categoria">
                                <span class="material-symbols-rounded" style="font-size: 18px;">delete</span>
                            </button>
                        </div>
                    </td>
                </tr>
            `);
        }

        // Subcategories
        if (typeFilter === 'all' || typeFilter === 'sub') {
            const listToRender = query ? matchedSubs : subs;
            listToRender.forEach(sub => {
                visibleCount++;
                renderedRows.push(`
                    <tr class="sub-row" data-id="${_esc(sub.id)}">
                        <td>
                            <div class="cat-cell-left" style="display: flex; align-items: center; gap: 8px;">
                                <span class="tree-branch">↳</span>
                                <span class="cat-name-text" style="font-weight: 500;">${_esc(sub.name)}</span>
                                <span class="badge-category badge-category--sub" style="margin-left: 8px;">Sotto</span>
                            </div>
                        </td>
                        <td>
                            <span class="cat-desc-text" style="color: var(--text-muted); font-size: 12px; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;">
                                ${_esc(sub.description || '')}
                            </span>
                        </td>
                        <td>
                            <span style="color: var(--text-muted); font-size: 11px; font-style: italic;">
                                Eredita da ${_esc(macro.name)}
                            </span>
                        </td>
                        <td>
                            <span style="color: var(--text-muted);">-</span>
                        </td>
                        <td>
                            <span class="count-badge">${sub.blueprint_count}</span>
                        </td>
                        <td>
                            <div class="action-buttons">
                                <button class="action-btn btn-edit-category" data-id="${_esc(sub.id)}" title="Modifica Sottocategoria">
                                    <span class="material-symbols-rounded" style="font-size: 18px;">edit</span>
                                </button>
                                <button class="action-btn action-btn--delete btn-delete-category" data-id="${_esc(sub.id)}" title="Elimina Sottocategoria">
                                    <span class="material-symbols-rounded" style="font-size: 18px;">delete</span>
                                </button>
                            </div>
                        </td>
                    </tr>
                `);
            });
        }
    });

    dom.statsCount.textContent = `${visibleCount} categorie`;

    if (renderedRows.length === 0) {
        dom.tbody.innerHTML = '';
        dom.empty.style.display = 'flex';
        return;
    }

    dom.empty.style.display = 'none';
    dom.tbody.innerHTML = renderedRows.join('');
}

function openCategoryModal(cat = null, defaultParentId = null) {
    editingCategoryId = cat ? cat.id : null;
    dom.modalCategoryTitle.textContent = cat ? 'Modifica Categoria' : (defaultParentId ? 'Nuova Sottocategoria' : 'Nuova Categoria');
    dom.inputName.value = cat ? cat.name : '';
    dom.inputDesc.value = cat ? cat.description : '';

    const parentIdToSelect = cat ? (cat.parent_id || '') : (defaultParentId || '');

    // Populate Parent Select (only macro categories can be parents)
    const availableParents = allCategories.filter(c => !c.parent_id && (!cat || c.id !== cat.id));
    dom.selectParent.innerHTML = `
        <option value="">Nessuna (Macro-categoria principale)</option>
        ${availableParents.map(p => `
            <option value="${_esc(p.id)}" ${p.id === parentIdToSelect ? 'selected' : ''}>
                ${_esc(p.name)}
            </option>
        `).join('')}
    `;

    // Populate Brands Checklist
    const selectedBrandIds = new Set(cat && cat.brand_ids ? cat.brand_ids : []);
    dom.brandsList.innerHTML = allBrands.map(b => {
        const isChecked = selectedBrandIds.has(b.id) ? 'checked' : '';
        return `
            <label class="checkbox-item">
                <input type="checkbox" value="${_esc(b.id)}" ${isChecked}>
                <span>${_esc(b.name)}</span>
            </label>
        `;
    }).join('');

    updateModalVisibility();

    dom.modalCategory.classList.add('active');
    setTimeout(() => dom.inputName.focus(), 100);
}

function updateModalVisibility() {
    const isMacro = !dom.selectParent.value;
    if (isMacro) {
        dom.sectionBrands.style.display = 'block';
        dom.subInheritHint.style.display = 'none';
    } else {
        dom.sectionBrands.style.display = 'none';
        dom.subInheritHint.style.display = 'block';
    }
}

function closeCategoryModal() {
    dom.modalCategory.classList.remove('active');
    editingCategoryId = null;
    dom.formCategory.reset();
}

async function handleSaveCategory(e) {
    e.preventDefault();
    const name = dom.inputName.value.trim();
    if (!name) {
        showToast('Inserisci il nome della categoria.', 'error');
        return;
    }

    const description = dom.inputDesc.value.trim();
    const parent_id = dom.selectParent.value || null;

    const checkedBrands = parent_id ? [] : Array.from(
        dom.brandsList.querySelectorAll('input[type="checkbox"]:checked')
    ).map(cb => cb.value);

    try {
        if (editingCategoryId) {
            await api.updateCategory(editingCategoryId, {
                name,
                description,
                parent_id,
                brand_ids: checkedBrands
            });
        } else {
            await api.createCategory({
                name,
                description,
                parent_id,
                brand_ids: checkedBrands
            });
            showToast('Nuova categoria creata con successo.');
        }
        closeCategoryModal();
        await loadData();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

function openDeleteModal(cat) {
    deletingCategory = cat;
    dom.deleteMsg.textContent = `Sei sicuro di voler eliminare la categoria "${cat.name}"?`;

    if (cat.subcategories_count > 0) {
        dom.deleteWarning.style.display = 'block';
        dom.deleteWarning.textContent = `⚠️ Impossibile eliminare: contiene ${cat.subcategories_count} sottocategorie. Rimuovi o sposta prima le sottocategorie.`;
        dom.btnConfirmDelete.disabled = true;
        dom.btnConfirmDelete.style.opacity = '0.5';
        dom.btnConfirmDelete.style.cursor = 'not-allowed';
    } else if (cat.blueprint_count > 0) {
        dom.deleteWarning.style.display = 'block';
        dom.deleteWarning.textContent = `⚠️ Impossibile eliminare: ci sono ${cat.blueprint_count} articoli associati a questa categoria.`;
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
    deletingCategory = null;
}

async function handleConfirmDelete() {
    if (!deletingCategory) return;
    try {
        await api.deleteCategory(deletingCategory.id);
        showToast('Categoria eliminata con successo.');
        closeDeleteModal();
        closeCategoryMobilePanel();
        await loadData();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

// Mobile Panel Logic
let currentMobileCategory = null;

function openCategoryMobilePanel(cat = null) {
    currentMobileCategory = cat;
    editingCategoryId = cat ? cat.id : null;

    if (dom.panelCategoryTitle) {
        dom.panelCategoryTitle.textContent = cat ? 'Modifica Categoria' : 'Aggiungi Categoria';
    }
    if (dom.btnPanelDelete) {
        dom.btnPanelDelete.style.visibility = cat ? 'visible' : 'hidden';
        dom.btnPanelDelete.style.display = cat ? 'inline-flex' : 'none';
    }

    const saveBtn = document.getElementById('btn-save-category-mobile');
    if (saveBtn) {
        saveBtn.innerHTML = cat
            ? '<span class="material-symbols-rounded">check</span> Salva Modifiche'
            : '<span class="material-symbols-rounded">add</span> Aggiungi Categoria';
    }

    if (dom.mobInputName) dom.mobInputName.value = cat ? cat.name : '';
    if (dom.mobInputDesc) dom.mobInputDesc.value = cat ? (cat.description || '') : '';

    const parentIdToSelect = cat ? (cat.parent_id || '') : '';
    const availableParents = allCategories.filter(c => !c.parent_id && (!cat || c.id !== cat.id));
    if (dom.mobSelectParent) {
        dom.mobSelectParent.innerHTML = `
            <option value="">Nessuna (Macro-categoria principale)</option>
            ${availableParents.map(p => `
                <option value="${_esc(p.id)}" ${p.id === parentIdToSelect ? 'selected' : ''}>
                    ${_esc(p.name)}
                </option>
            `).join('')}
        `;
    }

    const selectedBrandIds = new Set(cat && cat.brand_ids ? cat.brand_ids : []);
    if (dom.mobBrandsList) {
        dom.mobBrandsList.innerHTML = allBrands.map(b => {
            const isChecked = selectedBrandIds.has(b.id) ? 'checked' : '';
            return `
                <label class="checkbox-item">
                    <input type="checkbox" value="${_esc(b.id)}" ${isChecked}>
                    <span>${_esc(b.name)}</span>
                </label>
            `;
        }).join('');
    }

    updateMobilePanelVisibility();

    const page = document.querySelector('.pim-page');
    if (page) page.classList.add('mobile-panel-open');
    if (dom.mobInputName) setTimeout(() => dom.mobInputName.focus(), 150);
}

function updateMobilePanelVisibility() {
    if (!dom.mobSelectParent) return;
    const isMacro = !dom.mobSelectParent.value;
    if (isMacro) {
        if (dom.mobSectionBrands) dom.mobSectionBrands.style.display = 'flex';
        if (dom.mobSubInheritHint) dom.mobSubInheritHint.style.display = 'none';
    } else {
        if (dom.mobSectionBrands) dom.mobSectionBrands.style.display = 'none';
        if (dom.mobSubInheritHint) dom.mobSubInheritHint.style.display = 'block';
    }
}

function closeCategoryMobilePanel() {
    const page = document.querySelector('.pim-page');
    if (page) page.classList.remove('mobile-panel-open');
    currentMobileCategory = null;
    editingCategoryId = null;
    if (dom.formCategoryMobile) dom.formCategoryMobile.reset();
}

async function handleSaveCategoryMobile(e) {
    e.preventDefault();
    const name = dom.mobInputName ? dom.mobInputName.value.trim() : '';
    if (!name) {
        showToast('Inserisci il nome della categoria.', 'error');
        return;
    }

    const description = dom.mobInputDesc ? dom.mobInputDesc.value.trim() : '';
    const parent_id = (dom.mobSelectParent && dom.mobSelectParent.value) || null;

    const checkedBrands = parent_id || !dom.mobBrandsList ? [] : Array.from(
        dom.mobBrandsList.querySelectorAll('input[type="checkbox"]:checked')
    ).map(cb => cb.value);

    try {
        if (editingCategoryId) {
            await api.updateCategory(editingCategoryId, {
                name,
                description,
                parent_id,
                brand_ids: checkedBrands
            });
        } else {
            await api.createCategory({
                name,
                description,
                parent_id,
                brand_ids: checkedBrands
            });
            showToast('Nuova categoria creata con successo.');
        }
        closeCategoryMobilePanel();
        await loadData();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

// Event Listeners
document.addEventListener('DOMContentLoaded', () => {
    loadData();

    dom.searchInput.addEventListener('input', renderTable);
    dom.filterType.addEventListener('change', renderTable);

    dom.btnNew.addEventListener('click', () => openCategoryModal(null));
    if (dom.btnNewMobile) {
        dom.btnNewMobile.addEventListener('click', () => openCategoryMobilePanel(null));
    }
    dom.selectParent.addEventListener('change', updateModalVisibility);
    dom.btnCancelCategory.addEventListener('click', closeCategoryModal);
    dom.btnCloseCategory.addEventListener('click', closeCategoryModal);
    dom.formCategory.addEventListener('submit', handleSaveCategory);

    // Mobile Panel listeners
    if (dom.btnPanelBack) {
        dom.btnPanelBack.addEventListener('click', closeCategoryMobilePanel);
    }
    if (dom.btnPanelDelete) {
        dom.btnPanelDelete.addEventListener('click', () => {
            if (currentMobileCategory) {
                openDeleteModal(currentMobileCategory);
            }
        });
    }
    if (dom.mobSelectParent) {
        dom.mobSelectParent.addEventListener('change', updateMobilePanelVisibility);
    }
    if (dom.formCategoryMobile) {
        dom.formCategoryMobile.addEventListener('submit', handleSaveCategoryMobile);
    }

    dom.btnCancelDelete.addEventListener('click', closeDeleteModal);
    dom.btnCloseDelete.addEventListener('click', closeDeleteModal);
    dom.btnConfirmDelete.addEventListener('click', handleConfirmDelete);

    dom.tbody.addEventListener('click', (e) => {
        const addSubBtn = e.target.closest('.btn-add-sub');
        if (addSubBtn) {
            const parentId = addSubBtn.dataset.parentId;
            openCategoryModal(null, parentId);
            return;
        }

        const editBtn = e.target.closest('.btn-edit-category');
        if (editBtn) {
            const id = editBtn.dataset.id;
            const c = allCategories.find(x => x.id === id);
            if (c) openCategoryModal(c);
            return;
        }

        const deleteBtn = e.target.closest('.btn-delete-category');
        if (deleteBtn) {
            const id = deleteBtn.dataset.id;
            const c = allCategories.find(x => x.id === id);
            if (c) openDeleteModal(c);
            return;
        }

        // On mobile view: tapping anywhere on the category row opens dedicated edit panel
        if (window.innerWidth <= 768) {
            const row = e.target.closest('tr');
            if (row && row.dataset.id) {
                const c = allCategories.find(x => x.id === row.dataset.id);
                if (c) {
                    openCategoryMobilePanel(c);
                }
            }
        }
    });

    // Close on backdrop click
    dom.modalCategory.addEventListener('click', (e) => {
        if (e.target === dom.modalCategory) closeCategoryModal();
    });
    dom.modalDelete.addEventListener('click', (e) => {
        if (e.target === dom.modalDelete) closeDeleteModal();
    });
});
