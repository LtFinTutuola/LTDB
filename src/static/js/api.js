/**
 * api.js
 * Centralized HTTP client for all LTDB API endpoints.
 * All functions are async and throw Error with the server's detail message on failure.
 */

const API_BASE = '/api/v1';

async function _handleResponse(res) {
    if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try {
            const body = await res.json();
            detail = body.detail || detail;
        } catch { /* ignore parse error, use default */ }
        throw new Error(detail);
    }
    const ct = res.headers.get('Content-Type') || '';
    if (ct.includes('application/json')) return res.json();
    return res.text();
}

// --- Data Ingestion ---

export async function extractDDT(filePath, brandId) {
    const res = await fetch(`${API_BASE}/ingestion/extract`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file_path: filePath, brand_id: brandId }),
    });
    return _handleResponse(res);
}

export async function ingestSingleItem(payload) {
    const res = await fetch(`${API_BASE}/ingestion/single-item`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    });
    return _handleResponse(res);
}

export async function pollJobStatus(jobId) {
    const res = await fetch(`${API_BASE}/ingestion/extract/${encodeURIComponent(jobId)}`);
    return _handleResponse(res);
}

export async function submitRevisions(jobId, operations) {
    const res = await fetch(`${API_BASE}/ingestion/staging/${encodeURIComponent(jobId)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ operations }),
    });
    return _handleResponse(res);
}

export async function confirmIngestion(jobId) {
    const res = await fetch(`${API_BASE}/ingestion/confirm/${encodeURIComponent(jobId)}`, {
        method: 'POST',
    });
    return _handleResponse(res);
}

export async function retryPhoto(jobId, itemId, userFeedback = null, newUrl = null) {
    const res = await fetch(`${API_BASE}/ingestion/photo-retry/${encodeURIComponent(jobId)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ item_id: itemId, user_feedback: userFeedback, new_url: newUrl }),
    });
    return _handleResponse(res);
}

export function getPhotoUrl(photoId) {
    return `${API_BASE}/ingestion/photos/${encodeURIComponent(photoId)}`;
}

// --- Catalog ---

export async function semanticSearch(query, inStockOnly = false) {
    const res = await fetch(`${API_BASE}/catalog/search/semantic`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query, in_stock_only: inStockOnly }),
    });
    return _handleResponse(res);
}

export async function getBrands() {
    const res = await fetch(`${API_BASE}/catalog/brands`);
    return _handleResponse(res);
}

export async function getBrandsDetailed() {
    const res = await fetch(`${API_BASE}/catalog/brands/details`);
    return _handleResponse(res);
}

export async function createBrand(payload) {
    const res = await fetch(`${API_BASE}/catalog/brands`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    });
    return _handleResponse(res);
}

export async function updateBrand(brandId, payload) {
    const res = await fetch(`${API_BASE}/catalog/brands/${encodeURIComponent(brandId)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    });
    return _handleResponse(res);
}

export async function deleteBrand(brandId) {
    const res = await fetch(`${API_BASE}/catalog/brands/${encodeURIComponent(brandId)}`, {
        method: 'DELETE',
    });
    return _handleResponse(res);
}

export async function getCategoriesDetailed() {
    const res = await fetch(`${API_BASE}/catalog/categories`);
    return _handleResponse(res);
}

export async function createCategory(payload) {
    const res = await fetch(`${API_BASE}/catalog/categories`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    });
    return _handleResponse(res);
}

export async function updateCategory(categoryId, payload) {
    const res = await fetch(`${API_BASE}/catalog/categories/${encodeURIComponent(categoryId)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    });
    return _handleResponse(res);
}

export async function deleteCategory(categoryId) {
    const res = await fetch(`${API_BASE}/catalog/categories/${encodeURIComponent(categoryId)}`, {
        method: 'DELETE',
    });
    return _handleResponse(res);
}

export async function getBrandCategories(brandId) {
    const res = await fetch(`${API_BASE}/catalog/categories/${encodeURIComponent(brandId)}`);
    return _handleResponse(res);
}

export async function patchCatalogItem(blueprintId, fields) {
    const res = await fetch(`${API_BASE}/catalog/${encodeURIComponent(blueprintId)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(fields),
    });
    return _handleResponse(res);
}

export async function deleteCatalogItem(blueprintId, color = null) {
    let url = `${API_BASE}/catalog/${encodeURIComponent(blueprintId)}`;
    if (color) {
        url += `?color=${encodeURIComponent(color)}`;
    }
    const res = await fetch(url, {
        method: 'DELETE',
    });
    return _handleResponse(res);
}



// --- Excel Synchronization ---

export async function getPendingSales(targetDate = null) {
    let url = `${API_BASE}/excel-synchronization/pending-sales`;
    if (targetDate) url += `?target_date=${encodeURIComponent(targetDate)}`;
    const res = await fetch(url);
    return _handleResponse(res);
}

export async function getReconciliationCandidates(searchCode, isExchange = false) {
    const res = await fetch(`${API_BASE}/excel-synchronization/reconciliation-candidates?search_code=${encodeURIComponent(searchCode)}&is_exchange=${isExchange}`);
    return _handleResponse(res);
}

export async function reconcileSale(saleId, articleId) {
    const res = await fetch(`${API_BASE}/excel-synchronization/reconcile/${encodeURIComponent(saleId)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ article_id: articleId }),
    });
    return _handleResponse(res);
}

export async function pollExcelRows() {
    const res = await fetch(`${API_BASE}/excel-synchronization/poll-rows`, {
        method: 'POST',
    });
    return _handleResponse(res);
}

// --- Settings ---

export async function getSettings() {
    const res = await fetch(`${API_BASE}/settings`);
    return _handleResponse(res);
}

export async function updateSettings(payload) {
    const res = await fetch(`${API_BASE}/settings`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    });
    return _handleResponse(res);
}

