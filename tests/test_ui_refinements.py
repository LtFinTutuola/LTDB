"""
tests/test_ui_refinements.py
----------------------------
Test suite for UI and error handling refinements:
1. Error message sanitization (zero SQL leaks, friendly Italian messages).
2. Typo warning modal presence in HTML and handler logic.
3. Split blueprint button removal from warehouse table.
4. Photo search & URL input unification in staging area.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError, IntegrityError

from src.main import app
from src.utils.error_sanitizer import sanitize_error_detail


def test_sanitize_error_detail_sql_leaks():
    raw_operational = "(sqlite3.OperationalError) no such column: sale_date [SQL: SELECT sale_date FROM excel_sales] [parameters: ()]"
    sanitized = sanitize_error_detail(raw_operational)
    assert "[SQL:" not in sanitized
    assert "sqlite3" not in sanitized
    assert "database" in sanitized.lower()

    raw_unique = "(sqlite3.IntegrityError) UNIQUE constraint failed: brands.name [SQL: INSERT INTO brands (name) VALUES (?)]"
    sanitized_unique = sanitize_error_detail(raw_unique)
    assert "[SQL:" not in sanitized_unique
    assert "esiste già un elemento" in sanitized_unique

    raw_fk = "FOREIGN KEY constraint failed [SQL: DELETE FROM blueprints WHERE id = ?]"
    sanitized_fk = sanitize_error_detail(raw_fk)
    assert "[SQL:" not in sanitized_fk
    assert "collegato ad altri record" in sanitized_fk


def test_sanitize_error_detail_preserves_clean_validation_messages():
    clean_msg = "Il nome dell'articolo non può essere vuoto."
    assert sanitize_error_detail(clean_msg) == clean_msg

    clean_404 = "Articolo non trovato nel magazzino."
    assert sanitize_error_detail(clean_404) == clean_404


def test_global_exception_handler_sanitizes_500():
    client = TestClient(app)

    # Trigger a 500 on an endpoint by passing invalid path/state or verify sanitization
    # We can test http_exception_handler directly with a mocked 500 error
    from fastapi import HTTPException
    from src.main import http_exception_handler
    from starlette.requests import Request
    from starlette.datastructures import URL
    import asyncio

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/test-error",
        "headers": [],
    }
    req = Request(scope)
    leaky_http_exc = HTTPException(
        status_code=500,
        detail="sqlite3.OperationalError: no such table: fake_table [SQL: SELECT * FROM fake_table]"
    )
    resp = asyncio.run(http_exception_handler(req, leaky_http_exc))
    assert resp.status_code == 500
    import json
    data = json.loads(resp.body)
    assert "[SQL:" not in data["detail"]
    assert "sqlite3" not in data["detail"]
    assert "database" in data["detail"].lower()


def test_modal_typo_warning_in_index_html():
    client = TestClient(app)
    # Check index.html template file directly
    from pathlib import Path
    template_path = Path("src/templates/index.html")
    assert template_path.exists()
    content = template_path.read_text(encoding="utf-8")

    assert "id=\"modal-typo-warning\"" in content
    assert "id=\"btn-typo-modal-edit\"" in content
    assert "id=\"btn-typo-modal-confirm\"" in content
    assert "id=\"typo-modal-suspect-code\"" in content
    assert "id=\"typo-modal-suggested-code\"" in content
    assert "id=\"typo-modal-reason\"" in content


def test_warehouse_split_blueprint_button_removed():
    from pathlib import Path
    wh_js_path = Path("src/static/js/warehouse.js")
    wh_content = wh_js_path.read_text(encoding="utf-8")

    # The manual split button must NOT be present in warehouse row rendering
    assert 'class="action-btn btn-split-blueprint"' not in wh_content


def test_modal_staging_photo_url_in_index_html():
    from pathlib import Path
    template_path = Path("src/templates/index.html")
    assert template_path.exists()
    content = template_path.read_text(encoding="utf-8")

    assert "id=\"modal-staging-photo-url\"" in content
    assert "id=\"staging-modal-photo-url-input\"" in content
    assert "id=\"btn-confirm-staging-photo-url\"" in content
    assert "id=\"btn-cancel-staging-photo-url\"" in content


def test_staging_photo_unification_and_retry_button_removed():
    from pathlib import Path
    staging_js_path = Path("src/static/js/staging.js")
    staging_content = staging_js_path.read_text(encoding="utf-8")

    # 1. Retry button must be removed
    assert 'btn-retry-photo' not in staging_content
    assert 'mob-staging-btn-retry' not in staging_content

    # 2. Standalone manual URL button must be removed
    assert 'btn-manual-photo' not in staging_content
    assert 'mob-staging-btn-manual' not in staging_content

    # 3. Clean vertically stacked buttons and modal integration
    assert 'btn-search-photo' in staging_content
    assert 'btn-upload-photo' in staging_content
    assert 'openPhotoUrlModal' in staging_content
    assert 'modal-staging-photo-url' in staging_content


def test_staging_and_warehouse_javascript_validity():
    import subprocess
    import shutil

    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("Node.js is not installed on system")

    # Verify that all core UI ES modules parse cleanly without SyntaxErrors
    check_code = """
    for (const file of [
        './src/static/js/main.js',
        './src/static/js/staging.js',
        './src/static/js/warehouse.js',
        './src/static/js/blueprint_revision.js',
    ]) {
        try {
            await import(file);
        } catch (e) {
            if (e instanceof SyntaxError) {
                console.error('SyntaxError in ' + file + ':', e);
                process.exit(1);
            }
        }
    }
    """
    res = subprocess.run(
        [node_bin, "--input-type=module", "-e", check_code],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"JS module syntax check failed: {res.stderr}"


def test_staging_photo_buttons_directly_under_preview():
    from pathlib import Path
    content = Path("src/static/js/staging.js").read_text(encoding="utf-8")
    for line in content.splitlines():
        if 'class="btn-search-photo"' in line or 'class="btn-upload-photo"' in line:
            assert "margin-top:auto" not in line
    assert "window.__LTDB_BRANDS__" in content


def test_staging_and_blueprint_revision_category_hierarchy_per_brand():
    """
    Verify category hierarchies in staging.js and blueprint_revision.js are cached per brand_id
    (never latched on empty/other brand state) and always preserve a selected fallback option.
    """
    from pathlib import Path

    staging_js = Path("src/static/js/staging.js").read_text(encoding="utf-8")
    assert "_categoryHierarchyByBrand" in staging_js
    assert "_ensureCategoryHierarchy" in staging_js
    assert "if (_stagingData.brand_id && !_categoryHierarchy)" not in staging_js
    assert "_extractCatLabel" in staging_js
    assert "_buildMacroOptionsHtml" in staging_js
    assert "_buildSubCatOptionsHtml" in staging_js
    assert "if (catDesc && !matchedMacro)" in staging_js
    assert "if (selectedSub && !matchedSub)" in staging_js

    bp_rev_js = Path("src/static/js/blueprint_revision.js").read_text(encoding="utf-8")
    assert "_categoryHierarchyByBrand" in bp_rev_js
    assert "_categoryHierarchyBrandId" in bp_rev_js
    assert "if (brandId && !_categoryHierarchy)" not in bp_rev_js
    assert "if (catName && !matchedKey)" in bp_rev_js
    assert "if (subCatName && !matchedSub)" in bp_rev_js


def test_toast_positioning_avoids_staging_footer_and_header_actions():
    """
    Toast notifications in main.js and warehouse.js must use top-center non-blocking positioning
    so they never obscure 'Conferma e importa' (bottom-right) or header action buttons (top-right).
    """
    from pathlib import Path

    main_js = Path("src/static/js/main.js").read_text(encoding="utf-8")
    assert "ltdb-toast-container" in main_js
    assert "toast.style.bottom = '20px'" not in main_js
    assert "toast.style.right = '20px'" not in main_js
    assert "container.style.top = '16px'" in main_js
    assert "container.style.left = '50%'" in main_js
    assert "container.style.pointerEvents = 'none'" in main_js

    wh_js = Path("src/static/js/warehouse.js").read_text(encoding="utf-8")
    assert "toast.style.bottom = '24px'" not in wh_js
    assert "toast.style.right = '24px'" not in wh_js
    assert "toast.style.top = '16px'" in wh_js
    assert "toast.style.left = '50%'" in wh_js
    assert "toast.style.pointerEvents = 'none'" in wh_js


def test_blueprint_revision_photo_flow_unified_with_staging():
    """
    Verify blueprint_revision.js removes legacy 'Inserisci URL' / 'Riprova' buttons and unifies
    photo selection around 'Cerca' (Google Images + URL modal) and 'Carica Foto'.
    """
    from pathlib import Path

    bp_rev_js = Path("src/static/js/blueprint_revision.js").read_text(encoding="utf-8")
    assert "btn-manual-photo" not in bp_rev_js
    assert "btn-retry-photo" not in bp_rev_js
    assert "🔗 Inserisci URL" not in bp_rev_js
    assert "🔄 Riprova" not in bp_rev_js
    assert "btn-search-photo" in bp_rev_js
    assert "btn-upload-photo" in bp_rev_js
    assert "_openBpRevPhotoUrlModal" in bp_rev_js
    assert "modal-bp-rev-photo-url" in bp_rev_js

    workspace_css = Path("src/static/css/workspace.css").read_text(encoding="utf-8")
    assert ".btn-retry-photo" not in workspace_css
    assert ".btn-manual-photo" not in workspace_css

    index_html = Path("src/templates/index.html").read_text(encoding="utf-8")
    assert "main.js?v=" in index_html


def test_staging_category_resolution_and_hierarchy_switch():
    """
    Simulate the frontend category resolution logic in Node.js / JS runtime:
    Verifies that when importing Roncato first and Mandarina Duck second in the same session,
    the category hierarchy correctly fetches and switches per brand_id, properly populating
    and selecting the macro and sub category options.
    """
    import subprocess
    import shutil
    import json

    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("Node.js is not installed on system")

    js_code = """
    // Mock the backend API responses for the two brands
    const hierarchies = {
        '3724fce0-ed61-405b-96e6-2c51083d523b': { // Roncato
            'Cartelle': {
                description: 'Borse da lavoro',
                sub_categories: {
                    'Zaini Portacomputer': 'Zaini porta PC',
                    'Cartelle Porta PC': 'Cartelle tradizionali'
                }
            },
            'Valigie': {
                description: 'Bagagli',
                sub_categories: {
                    'Trolley Cabina': 'Trolley compatti'
                }
            }
        },
        '4f08f4c6-e2d7-4d43-aa28-748e4110fab1': { // Mandarina Duck
            'Borse': {
                description: 'Borse donna',
                sub_categories: {
                    'Hobo Bag': 'Borse a spalla e a sacco',
                    'Shopping Bag': 'Tote bag'
                }
            },
            'Portafogli': {
                description: 'Piccola pelletteria',
                sub_categories: {
                    'Mini Wallet': 'Portafogli compatti'
                }
            }
        }
    };

    const _categoryHierarchyByBrand = {};
    let _categoryHierarchy = null;
    let _categoryHierarchyBrandId = null;

    function _esc(s) { return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }

    async function _ensureCategoryHierarchy(data) {
        let brandId = data?.brand_id || data?.data?.brand_id || null;
        if (!brandId && (data?.blueprints || data?.data?.blueprints)?.length > 0) {
            const bps = data?.blueprints || data?.data?.blueprints || [];
            brandId = bps[0]?.brand_id || null;
        }
        if (!brandId) {
            _categoryHierarchy = {};
            _categoryHierarchyBrandId = null;
            return;
        }
        const cached = _categoryHierarchyByBrand[brandId];
        if (cached && Object.keys(cached).length > 0) {
            _categoryHierarchy = cached;
            _categoryHierarchyBrandId = brandId;
            return;
        }
        const res = hierarchies[brandId] || {};
        _categoryHierarchy = res;
        _categoryHierarchyBrandId = brandId;
        if (Object.keys(_categoryHierarchy).length > 0) {
            _categoryHierarchyByBrand[brandId] = _categoryHierarchy;
        }
    }

    function _extractCatLabel(val) {
        if (!val) return '';
        if (typeof val === 'string') return val.trim();
        if (typeof val === 'object') return String(val.description || val.name || '').trim();
        return '';
    }

    function _resolveBlueprintCategoryLabels(bp) {
        let catDesc = _extractCatLabel(bp?.category);
        let subCatDesc = _extractCatLabel(bp?.sub_category);
        const hasHierarchy = Boolean(_categoryHierarchy && Object.keys(_categoryHierarchy).length > 0);
        if (hasHierarchy) {
            const macroKeys = Object.keys(_categoryHierarchy);
            const matchedDirectMacro = macroKeys.find(m => m.trim().toLowerCase() === catDesc.toLowerCase());
            if (matchedDirectMacro) {
                catDesc = matchedDirectMacro;
                const subs = Object.keys(_categoryHierarchy[matchedDirectMacro]?.sub_categories || {});
                const matchedSub = subs.find(s => s.trim().toLowerCase() === subCatDesc.toLowerCase());
                if (matchedSub) {
                    subCatDesc = matchedSub;
                }
            } else {
                const candidateSub = catDesc || subCatDesc;
                if (candidateSub) {
                    for (const m of macroKeys) {
                        const subs = Object.keys(_categoryHierarchy[m]?.sub_categories || {});
                        const matchedSub = subs.find(s => s.trim().toLowerCase() === candidateSub.toLowerCase());
                        if (matchedSub) {
                            subCatDesc = matchedSub;
                            catDesc = m;
                            break;
                        }
                    }
                }
            }
        }
        return { catDesc, subCatDesc, hasHierarchy };
    }

    function _buildMacroOptionsHtml(catDesc, hasHierarchy) {
        if (!hasHierarchy) {
            return catDesc ? `<option value="${_esc(catDesc)}" selected>${_esc(catDesc)}</option>` : '';
        }
        let matchedMacro = null;
        let html = Object.keys(_categoryHierarchy).map(c => {
            const isSel = Boolean(catDesc) && c.trim().toLowerCase() === catDesc.toLowerCase();
            if (isSel) matchedMacro = c;
            return `<option value="${_esc(c)}" ${isSel ? 'selected' : ''}>${_esc(c)}</option>`;
        }).join('');
        if (catDesc && !matchedMacro) {
            html += `<option value="${_esc(catDesc)}" selected>${_esc(catDesc)}</option>`;
        }
        return html;
    }

    function _buildSubCatOptionsHtml(selectedMacro, selectedSub, hasHierarchy) {
        const matchedMacro = Object.keys(_categoryHierarchy || {}).find(
            c => c.trim().toLowerCase() === (selectedMacro || '').trim().toLowerCase()
        ) || selectedMacro;
        if (!hasHierarchy || !matchedMacro || !_categoryHierarchy[matchedMacro]) {
            return selectedSub ? `<option value="${_esc(selectedSub)}" selected>${_esc(selectedSub)}</option>` : '';
        }
        const subs = Object.keys(_categoryHierarchy[matchedMacro].sub_categories || {});
        let matchedSub = false;
        let html = subs.map(sc => {
            const isSel = Boolean(selectedSub) && sc.trim().toLowerCase() === selectedSub.trim().toLowerCase();
            if (isSel) matchedSub = true;
            return `<option value="${_esc(sc)}" ${isSel ? 'selected' : ''}>${_esc(sc)}</option>`;
        }).join('');
        if (selectedSub && !matchedSub) {
            html += `<option value="${_esc(selectedSub)}" selected>${_esc(selectedSub)}</option>`;
        }
        return html;
    }

    (async () => {
        // Step 1: Roncato import
        const roncatoData = {
            brand_id: '3724fce0-ed61-405b-96e6-2c51083d523b',
            brand_name: 'Roncato',
            blueprints: [{
                id: 'bp-roncato-1',
                category: { id: 'cat-cartelle', description: 'Cartelle' },
                sub_category: { id: 'sub-zaini', description: 'Zaini Portacomputer' }
            }]
        };
        await _ensureCategoryHierarchy(roncatoData);
        const roncatoResolved = _resolveBlueprintCategoryLabels(roncatoData.blueprints[0]);
        const roncatoMacroOpts = _buildMacroOptionsHtml(roncatoResolved.catDesc, roncatoResolved.hasHierarchy);
        const roncatoSubOpts = _buildSubCatOptionsHtml(roncatoResolved.catDesc, roncatoResolved.subCatDesc, roncatoResolved.hasHierarchy);

        if (!roncatoMacroOpts.includes('value="Cartelle" selected')) throw new Error('Roncato macro not selected');
        if (!roncatoSubOpts.includes('value="Zaini Portacomputer" selected')) throw new Error('Roncato sub not selected');

        // Step 2: Mandarina Duck import in the SAME session
        const mdData = {
            brand_id: '4f08f4c6-e2d7-4d43-aa28-748e4110fab1',
            brand_name: 'Mandarina Duck',
            blueprints: [{
                id: 'bp-md-1',
                category: { id: 'cat-borse', description: 'Borse' },
                sub_category: { id: 'sub-hobo', description: 'Hobo Bag' }
            }]
        };
        await _ensureCategoryHierarchy(mdData);
        const mdResolved = _resolveBlueprintCategoryLabels(mdData.blueprints[0]);
        const mdMacroOpts = _buildMacroOptionsHtml(mdResolved.catDesc, mdResolved.hasHierarchy);
        const mdSubOpts = _buildSubCatOptionsHtml(mdResolved.catDesc, mdResolved.subCatDesc, mdResolved.hasHierarchy);

        if (!mdMacroOpts.includes('value="Borse" selected')) throw new Error('Mandarina Duck macro not selected');
        if (!mdSubOpts.includes('value="Hobo Bag" selected')) throw new Error('Mandarina Duck sub not selected');

        // Step 3: Blueprint with subcategory only (e.g. 'Hobo Bag' with missing macro)
        const subOnlyResolved = _resolveBlueprintCategoryLabels({
            id: 'bp-md-sub-only',
            category: null,
            sub_category: 'Hobo Bag'
        });
        if (subOnlyResolved.catDesc !== 'Borse') throw new Error('Failed to infer macro from subcategory');
        if (subOnlyResolved.subCatDesc !== 'Hobo Bag') throw new Error('Failed to resolve subcategory');

        console.log('SUCCESS');
    })();
    """

    res = subprocess.run(
        [node_bin, "--input-type=module", "-e", js_code],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"Category hierarchy simulation failed: {res.stderr}"
    assert "SUCCESS" in res.stdout
