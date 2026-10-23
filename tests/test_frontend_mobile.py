"""
tests/test_frontend_mobile.py
------------------------------
Verifies that all frontend pages and assets include the responsive mobile
optimizations, viewport meta tag, mobile navigation bars, and mobile CSS rules,
ensuring seamless compatibility between desktop and mobile environments.
"""
from fastapi.testclient import TestClient
from src.main import app


def test_mobile_viewport_and_navigation_across_pages(db_session):
    """
    Verify that all key SPA pages render the responsive viewport tag with viewport-fit=cover
    and include the mobile topbar and mobile bottom navigation with all 5 main links.
    """
    client = TestClient(app)
    pages = [
        ("/vendite", "Vendite"),
        ("/magazzino", "Magazzino"),
        ("/brands", "Brands"),
        ("/categories", "Categorie"),
        ("/settings", "Impostazioni"),
    ]

    for route, expected_title in pages:
        res = client.get(route)
        assert res.status_code == 200, f"Page {route} failed with status {res.status_code}"
        html = res.text

        # 1. Viewport meta tag
        assert '<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">' in html, (
            f"Page {route} missing viewport-fit=cover meta tag"
        )

        # 2. Mobile topbar contains only logo and page heading (no 'La Triestina', no admin chip)
        assert 'id="mobile-topbar"' in html, f"Page {route} missing mobile topbar"
        topbar_start = html.find('id="mobile-topbar"')
        topbar_end = html.find('</header>', topbar_start)
        topbar_html = html[topbar_start:topbar_end]
        assert 'La Triestina' not in topbar_html, f"Page {route} topbar still contains 'La Triestina'"
        assert 'chip--admin' not in topbar_html, f"Page {route} topbar still contains admin chip"
        assert 'class="mobile-topbar__logo"' in topbar_html, f"Page {route} topbar missing logo"
        assert 'class="mobile-topbar__page-title"' in topbar_html, f"Page {route} topbar missing page title"

        # 3. Mobile bottom navigation with 5 routes
        assert 'id="mobile-bottom-nav"' in html, f"Page {route} missing mobile bottom nav"
        assert 'href="/vendite"' in html
        assert 'href="/magazzino"' in html
        assert 'href="/brands"' in html
        assert 'href="/categories"' in html
        assert 'href="/settings"' in html

        # 4. Material symbols for mobile navigation
        assert "point_of_sale" in html
        assert "inventory_2" in html
        assert "style" in html
        assert "category" in html
        assert "settings" in html


def test_vendite_mobile_controls_rendered(db_session):
    """
    Verify that the Vendite page includes the mobile Gemini trigger button
    and the mobile back button for returning from the work panel.
    """
    client = TestClient(app)
    res = client.get("/vendite")
    assert res.status_code == 200
    html = res.text

    assert 'id="btn-mobile-open-gemini"' in html
    assert 'class="btn-mobile-gemini-trigger"' in html
    assert 'id="btn-mobile-back-to-table"' in html
    assert 'id="mobile-panel-backdrop"' in html
    assert 'class="btn-mobile-back"' in html


def test_responsive_css_rules_served():
    """
    Verify that layout.css, sidebar.css, sales_table.css, and pim_crud.css
    contain responsive media queries and mobile rules.
    """
    client = TestClient(app)

    # 1. layout.css
    res_layout = client.get("/static/css/layout.css")
    assert res_layout.status_code == 200
    assert "100dvh" in res_layout.text
    assert "@media (max-width: 1024px)" in res_layout.text

    # 2. sidebar.css
    res_sidebar = client.get("/static/css/sidebar.css")
    assert res_sidebar.status_code == 200
    assert ".mobile-bottom-nav" in res_sidebar.text
    assert ".mobile-topbar" in res_sidebar.text
    assert "@media (max-width: 1024px)" in res_sidebar.text
    assert "@media (min-width: 1025px)" in res_sidebar.text

    # 3. sales_table.css
    res_sales = client.get("/static/css/sales_table.css")
    assert res_sales.status_code == 200
    assert "mobile-panel-open" in res_sales.text
    assert ".btn-mobile-gemini-trigger" in res_sales.text
    assert ".mobile-panel-topbar" in res_sales.text
    assert "@media (max-width: 768px)" in res_sales.text

    # 4. pim_crud.css
    res_pim = client.get("/static/css/pim_crud.css")
    assert res_pim.status_code == 200
    assert "@media (max-width: 768px)" in res_pim.text


def test_sales_page_js_mobile_handlers():
    """
    Verify that sales_page.js contains handlers for mobile-panel-open.
    """
    client = TestClient(app)
    res_js = client.get("/static/js/sales_page.js")
    assert res_js.status_code == 200
    assert "btnMobileOpenGemini" in res_js.text
    assert "btnMobileBackToTable" in res_js.text
    assert "mobile-panel-open" in res_js.text


def test_categories_mobile_elements_rendered(db_session):
    """
    Verify that /categories renders the mobile elements:
    - Small '+' mobile button in the toolbar
    - Dedicated mobile category panel with back button, title, delete button, and form
    """
    client = TestClient(app)
    res = client.get("/categories")
    assert res.status_code == 200
    html = res.text

    # 1. Container has categories-page class
    assert 'class="pim-page categories-page"' in html

    # 2. Add category mobile button sitting in toolbar row
    assert 'id="btn-new-category-mobile"' in html
    assert 'class="btn-add-category-mobile"' in html

    # 3. Dedicated mobile panel structure
    assert 'id="category-mobile-panel"' in html
    assert 'id="btn-category-panel-back"' in html
    assert 'id="category-panel-title"' in html
    assert 'id="btn-category-panel-delete"' in html
    assert 'id="form-category-mobile"' in html
    assert 'id="mob-cat-name"' in html
    assert 'id="mob-cat-description"' in html
    assert 'id="mob-cat-parent"' in html
    assert 'id="btn-save-category-mobile"' in html


def test_categories_mobile_css_and_js():
    """
    Verify that pim_crud.css and categories_page.js contain all mobile rules
    and logic for categories, including 2-row layout, centered header, and discreet delete button.
    """
    client = TestClient(app)

    # 1. pim_crud.css
    res_css = client.get("/static/css/pim_crud.css")
    assert res_css.status_code == 200
    css = res_css.text
    assert ".categories-page" in css
    assert ".btn-add-category-mobile" in css
    assert ".category-mobile-panel" in css
    assert "mobile-panel-open" in css
    assert ".cat-type-icon--macro" in css
    assert ".cat-name-text" in css
    assert ".cat-desc-text" in css
    assert "flex-direction: column" in css
    assert "left: 50%" in css
    assert "top: 50%" in css
    assert ".category-panel-btn-delete" in css

    # 2. categories_page.js
    res_js = client.get("/static/js/categories_page.js")
    assert res_js.status_code == 200
    js = res_js.text
    assert "openCategoryMobilePanel" in js
    assert "closeCategoryMobilePanel" in js
    assert "handleSaveCategoryMobile" in js
    assert "btn-new-category-mobile" in js
    assert "cat-type-icon" in js
    assert "mobile-panel-open" in js
    assert "Aggiungi Categoria" in js
    assert "Modifica Categoria" in js


def test_brands_mobile_elements_rendered(db_session):
    """
    Verify that /brands renders the mobile elements:
    - Small '+' mobile button in the toolbar
    - Dedicated mobile brand panel with back button, title, delete button, and form
    """
    client = TestClient(app)
    res = client.get("/brands")
    assert res.status_code == 200
    html = res.text

    # 1. Container has brands-page class
    assert 'class="pim-page brands-page"' in html

    # 2. Add brand mobile button sitting in toolbar row
    assert 'id="btn-new-brand-mobile"' in html
    assert 'class="btn-add-brand-mobile"' in html

    # 3. Dedicated mobile panel structure
    assert 'id="brand-mobile-panel"' in html
    assert 'id="btn-brand-panel-back"' in html
    assert 'id="brand-panel-title"' in html
    assert 'id="btn-brand-panel-delete"' in html
    assert 'id="form-brand-mobile"' in html
    assert 'id="mob-brand-name"' in html
    assert 'id="mob-brand-categories-list"' in html
    assert 'id="btn-save-brand-mobile"' in html


def test_brands_mobile_css_and_js():
    """
    Verify that pim_crud.css and brands_page.js contain all mobile rules
    and logic for brands, including 2-row layout with category chips, centered header, and discreet delete button.
    """
    client = TestClient(app)

    # 1. pim_crud.css
    res_css = client.get("/static/css/pim_crud.css")
    assert res_css.status_code == 200
    css = res_css.text
    assert ".brands-page" in css
    assert ".btn-add-brand-mobile" in css
    assert ".brand-mobile-panel" in css
    assert ".brand-name-text" in css
    assert ".brand-panel-title" in css
    assert ".brand-panel-btn-delete" in css

    # 2. brands_page.js
    res_js = client.get("/static/js/brands_page.js")
    assert res_js.status_code == 200
    js = res_js.text
    assert "openBrandMobilePanel" in js
    assert "closeBrandMobilePanel" in js
    assert "handleSaveBrandMobile" in js
    assert "btn-new-brand-mobile" in js
    assert "Aggiungi Brand" in js
    assert "Modifica Brand" in js


def test_magazzino_mobile_toolbar_elements(db_session):
    """
    Verify that /magazzino renders the mobile-optimized single-row toolbar elements:
    - Search toolbar containing toggle, search bar, and search button
    - Compact toggle containing inventory icon, slider, and label
    - Search button with icon and text span
    - Mobile CSS rules maintaining single-row layout and hiding button text and toggle label on mobile
    """
    client = TestClient(app)
    res = client.get("/magazzino")
    assert res.status_code == 200
    html = res.text

    # 1. Markup verification
    assert 'class="search-toolbar"' in html
    assert 'id="warehouse-stock-toggle"' in html
    assert 'warehouse-toggle__icon' in html
    assert 'inventory_2' in html
    assert 'warehouse-toggle__label' in html
    assert 'id="warehouse-search-input"' in html
    assert 'id="warehouse-search-btn"' in html

    # Verify inverted order: search input precedes stock toggle in the toolbar
    search_pos = html.find('id="warehouse-search-input"')
    toggle_pos = html.find('id="warehouse-stock-toggle"')
    assert search_pos != -1 and toggle_pos != -1
    assert search_pos < toggle_pos, "Expected search input to precede stock toggle in toolbar markup"

    # 2. Desktop CSS verification (toggle icon hidden)
    assert '.warehouse-toggle__icon {\n    display: none;\n  }' in html

    # 3. Mobile CSS verification (@media (max-width: 768px))
    assert 'flex-direction: row;' in html
    assert '.warehouse-toggle__label {\n      display: none;\n    }' in html
    assert '.search-btn span:not(.material-symbols-rounded) {\n      display: none;\n    }' in html


def test_magazzino_mobile_articles_list_and_panel(db_session):
    """
    Verify that /magazzino renders:
    - Tabs formatting where each tab takes exactly 1/3 of the space
    - Dedicated mobile article panel with header (back, centered title, delete), scrollable body, and full form
    - Mobile CSS rules: table headers hidden, table as non-card list with reduced padding, 3-row content (brand+cat, title, stock)
    - Warehouse JS exports and references for mobile panel and 3-row list rendering
    """
    client = TestClient(app)
    res = client.get("/magazzino")
    assert res.status_code == 200
    html = res.text

    # 1. 1/3 tabs formatting
    assert '#magazzino-tabs .tab {' in html
    assert 'width: 33.333% !important;' in html
    assert 'flex: 1 1 0 !important;' in html

    # 2. Dedicated mobile article panel in markup
    assert 'id="article-mobile-panel"' in html
    assert 'id="btn-article-panel-back"' in html
    assert 'id="article-panel-title"' in html
    assert 'id="btn-article-panel-delete"' in html
    assert 'id="form-article-mobile"' in html
    assert 'id="mob-art-photo-wrap"' in html
    assert 'id="mob-art-photo"' in html
    assert 'id="mob-art-brand"' in html
    assert 'id="mob-art-stock"' in html
    assert 'id="mob-art-name"' in html
    assert 'id="mob-art-vendor-code"' in html
    assert 'id="mob-art-barcode"' in html
    assert 'id="mob-art-description"' in html
    assert 'id="mob-art-ext-description"' in html
    assert 'id="mob-art-tags"' in html
    assert 'id="mob-art-materials"' in html
    assert 'id="btn-save-article-mobile"' in html

    # 3. CSS rules for mobile list and scrollable panel
    assert '.warehouse-table thead {\n      display: none !important;\n    }' in html
    assert 'tr.warehouse-row {\n      display: flex;' in html
    assert 'border-bottom: 1px solid var(--border-color);' in html
    assert 'background: transparent;' in html
    assert 'td.warehouse-row__cell--photo' in html
    assert 'td.warehouse-row__cell--info' in html
    assert '.warehouse-row__mobile-meta-top' in html
    assert '.warehouse-row__mobile-brand' in html
    assert '.warehouse-row__mobile-category' in html
    assert '.warehouse-row__title' in html
    assert '.warehouse-row__mobile-meta-bottom' in html
    assert '.article-mobile-panel' in html
    assert '.magazzino-container.mobile-panel-open .article-mobile-panel' in html
    assert 'overflow-y: auto !important;' in html
    assert '.warehouse-desktop-only {\n      display: none !important;\n    }' in html

    # 4. Warehouse JS verification
    res_js = client.get("/static/js/warehouse.js")
    assert res_js.status_code == 200
    js = res_js.text
    assert "warehouse-row__cell--photo" in js
    assert "warehouse-row__cell--info" in js
    assert "warehouse-row__mobile-meta-top" in js
    assert "warehouse-row__mobile-meta-bottom" in js
    assert "warehouse-desktop-only" in js
    assert "openArticleMobilePanel" in js
    assert "closeArticleMobilePanel" in js
    assert "_handleSaveArticleMobile" in js
    assert "btn-article-panel-back" in js
    assert "btn-article-panel-delete" in js


def test_staging_mobile_elements_and_css(db_session):
    """
    Verify that workspace.css and staging.js contain the mobile responsive rules
    and markup structure for staging items (single article import and DDT import):
    - Flat continuous mobile list (.staging-mobile-list, .staging-mobile-item)
    - Dedicated slide-in edit/delete panel (#staging-mobile-panel, .staging-mobile-panel)
    - Mobile panel topbar with back button, centered title, discreet delete button
    - Scrollable form with full fields (name, categories, codes, quantity, colors, descriptions, tags, materials)
    - Photo proposal actions (Google search, manual URL, AI retry)
    - Operations queue support for delete_item and update mutations
    """
    client = TestClient(app)

    # 1. workspace.css rules
    res_css = client.get("/static/css/workspace.css")
    assert res_css.status_code == 200
    css = res_css.text

    assert ".staging-desktop-view" in css
    assert ".staging-mobile-view" in css
    assert ".staging-mobile-panel" in css
    assert ".btn-close-panel--mobile" in css
    assert ".btn-close-panel--desktop" in css
    assert "@media (max-width: 768px)" in css
    assert ".staging-mobile-list" in css
    assert ".staging-mobile-item" in css
    assert "mobile-staging-panel-open" in css
    assert "transform: translateX(100%)" in css
    assert "transform: translateX(0)" in css

    # 2. staging.js functions and template markup
    res_js = client.get("/static/js/staging.js")
    assert res_js.status_code == 200
    js = res_js.text

    assert "openStagingMobilePanel" in js
    assert "closeStagingMobilePanel" in js
    assert "_buildMobileStagingList" in js
    assert "_buildMobileStagingPanel" in js
    assert "staging-mobile-item" in js
    assert "staging-mobile-panel" in js
    assert "btn-staging-mobile-back" in js
    assert "staging-mobile-title" in js
    assert "btn-staging-mobile-delete" in js
    assert "form-staging-mobile" in js
    assert "mob-staging-photo" in js
    assert "mob-staging-btn-search" in js
    assert "mob-staging-btn-manual" in js
    assert "mob-staging-btn-retry" in js
    assert "mob-staging-name" in js
    assert "mob-staging-macro-cat" in js
    assert "mob-staging-sub-cat" in js
    assert "mob-staging-vendor-code" in js
    assert "mob-staging-barcode" in js
    assert "mob-staging-qty" in js
    assert "mob-staging-colors" in js
    assert "mob-staging-desc" in js
    assert "mob-staging-ext-desc" in js
    assert "mob-staging-tags" in js
    assert "mob-staging-materials" in js
    assert "btn-save-staging-mobile" in js
    assert "_handleMobileSave" in js
    assert "_handleMobileDelete" in js
    assert "_enqueueDelete" in js
    assert "delete_item" in js


def test_vendite_four_columns_and_gemini_article_card(db_session):
    """
    Verify the dual layout for Vendite:
    1. Desktop: full 11-column table (#sales-table) and tabs (Vendite, Giornata, Storico) under .sales-desktop-tabs.
    2. Mobile: simplified 4-column table (#sales-table-mobile) and mobile title under .sales-mobile-title.
    3. CSS defines media queries toggling desktop vs mobile tables and controls.
    4. JS supports both desktop 11 columns and mobile 4 columns with Gemini preview card visualization.
    """
    client = TestClient(app)

    # 1. Template check
    res = client.get("/vendite")
    assert res.status_code == 200
    html = res.text

    # Desktop table
    assert 'id="sales-table"' in html
    desktop_table_html = html.split('id="sales-table"')[1].split('</table>')[0]
    assert '<th class="col-status">#</th>' in desktop_table_html
    assert '<th class="col-price">PL</th>' in desktop_table_html
    assert '<th class="col-cat">B</th>' in desktop_table_html
    assert '<th class="col-cat">Sam/AT</th>' in desktop_table_html
    assert '<th class="col-cat">V</th>' in desktop_table_html
    assert '<th class="col-cat">PP</th>' in desktop_table_html
    assert '<th class="col-cat">C</th>' in desktop_table_html
    assert '<th class="col-cat">CAR</th>' in desktop_table_html
    assert '<th class="col-cat">BORSELLI</th>' in desktop_table_html
    assert '<th class="col-cat">O+A</th>' in desktop_table_html
    assert '<th class="col-recon">Articolo</th>' in desktop_table_html

    # Mobile table
    assert 'id="sales-table-mobile"' in html
    mobile_table_html = html.split('id="sales-table-mobile"')[1].split('</table>')[0]
    assert '<th class="col-price-list">Prezzo di listino</th>' in mobile_table_html
    assert '<th class="col-price-final">Prezzo scontato</th>' in mobile_table_html
    assert '<th class="col-excel-col">Colonna excel</th>' in mobile_table_html
    assert '<th class="col-article">Articolo</th>' in mobile_table_html

    # Tabs and titles
    assert 'sales-desktop-tabs' in html
    assert '<button class="tab" data-tab="stats">Giornata</button>' in html
    assert '<button class="tab" data-tab="history">Storico</button>' in html
    assert 'sales-mobile-title' in html
    assert 'Vendite di giornata' in html

    # 2. CSS check
    res_css = client.get("/static/css/sales_table.css")
    assert res_css.status_code == 200
    css = res_css.text
    assert ".sales-desktop-tabs" in css
    assert ".sales-mobile-title" in css
    assert ".sales-table--desktop" in css
    assert ".sales-table--mobile" in css
    assert ".col-price-list" in css
    assert ".col-price-final" in css
    assert ".col-excel-col" in css
    assert ".col-article" in css
    assert ".search-preview-card--sale" in css
    assert "@media (min-width: 769px)" in css
    assert ".mobile-panel-backdrop" in css
    assert ".sales-table tbody tr" in css
    assert ".sales-table--desktop tbody tr" in css
    assert ".sales-table--mobile tbody tr" in css
    assert ".btn-add-recon:disabled" in css
    assert ".search-preview-card--sale .preview-card__photo" in css
    assert ".search-preview-card--sale .preview-card__meta" in css

    # 3. JS check
    res_js = client.get("/static/js/sales_page.js")
    assert res_js.status_code == 200
    js = res_js.text
    assert "search-preview-card--sale" in js
    assert "preview-card__photo" in js
    assert "preview-card__name" in js
    assert "preview-card__brand" in js
    assert "preview-card__cat" in js
    assert "preview-card__chevron" in js
    assert "col-price-list" in js
    assert "col-price-final" in js
    assert "col-excel-col" in js
    assert "col-article" in js
    assert "sales-table-mobile" in js
    assert "sales-tbody-mobile" in js
    assert "renderDesktopReconCell" in js
    assert "renderMobileReconCard" in js
    assert "btn-add-recon--disabled" in js
    assert "btn.disabled = true" in js


def test_magazzino_camera_barcode_elements(db_session):
    """
    Verify that the Magazzino page includes:
    1. The camera barcode scan button in the search toolbar.
    2. The hidden camera file input with capture="environment" for plain HTTP non-SSL camera access.
    3. The mobile article panel barcode scan button.
    4. The barcode_scanner.js module and its decoding methods.
    """
    client = TestClient(app)

    # 1. Page template check
    res = client.get("/magazzino")
    assert res.status_code == 200
    html = res.text

    assert 'id="warehouse-barcode-scan-btn"' in html
    assert 'id="warehouse-search-scan-btn"' in html
    assert 'id="warehouse-barcode-camera-input"' in html
    assert 'capture="environment"' in html
    assert 'accept="image/*"' in html
    assert 'id="btn-scan-mob-art-barcode"' in html
    assert 'class="search-scan-btn"' in html

    # 2. JS module check
    res_js = client.get("/static/js/barcode_scanner.js")
    assert res_js.status_code == 200
    js = res_js.text

    assert "capture" in js
    assert "environment" in js
    assert "decodeBarcodeFromFile" in js
    assert "decodeBarcodeFromCanvas" in js
    assert "setupBarcodeScanner" in js
    assert "BarcodeDetector" in js
    assert "/api/v1/catalog/scan-barcode" in js


def test_single_article_import_camera_barcode_scanner(db_session):
    """
    Verify that single article import includes camera barcode scanning functionality
    that is strictly active/visible only within the mobile layout:
    1. CSS rules in index.html hiding .btn-scan-field--mobile-only on desktop and enabling it on mobile.
    2. imports.js single article import form contains the scan button with .btn-scan-field--mobile-only
       and wires setupBarcodeScanner to populate #single-barcode.
    3. staging.js mobile panel contains the scan button for editing barcode in staging.
    """
    client = TestClient(app)

    # 1. Desktop & Mobile CSS rules in /magazzino template
    res = client.get("/magazzino")
    assert res.status_code == 200
    html = res.text

    assert '.btn-scan-field--mobile-only {\n    display: none !important;\n  }' in html
    assert '.btn-scan-field--mobile-only {\n      display: inline-flex !important;' in html
    assert '.barcode-input-wrapper {' in html

    # 2. imports.js single article form
    res_imports = client.get("/static/js/imports.js")
    assert res_imports.status_code == 200
    imports_js = res_imports.text

    assert 'btn-scan-single-barcode' in imports_js
    assert 'btn-scan-field--mobile-only' in imports_js
    assert 'setupBarcodeScanner' in imports_js
    assert 'single-barcode-camera-input' in imports_js
    assert 'single-barcode' in imports_js

    # 3. staging.js mobile staging panel
    res_staging = client.get("/static/js/staging.js")
    assert res_staging.status_code == 200
    staging_js = res_staging.text

    assert 'btn-scan-mob-staging-barcode' in staging_js
    assert 'mob-staging-barcode-camera-input' in staging_js
    assert 'setupBarcodeScanner' in staging_js


def test_materials_optional_in_warehouse_forms_and_api(db_session):
    """
    Verify that materials are optional both in the warehouse edit forms (desktop and mobile)
    and in the catalog patch API, aligned with staging.
    """
    client = TestClient(app)

    # 1. Template check: no required attribute and no asterisk on materials fields
    res = client.get("/magazzino")
    assert res.status_code == 200
    html = res.text

    assert '<label for="edit-article-materials" style="font-size: 13px; font-weight: 500; color: var(--text-secondary);">Materiali (separati da virgola)</label>' in html
    assert '<input type="text" id="edit-article-materials" class="form-control" placeholder="Es. ecopelle, metallo">' in html
    assert '<input type="text" id="edit-article-materials" class="form-control" placeholder="Es. ecopelle, metallo" required>' not in html

    assert '<label for="mob-art-materials" style="font-size: 13px; font-weight: 500; color: var(--text-secondary);">Materiali (separati da virgola)</label>' in html
    assert '<input type="text" id="mob-art-materials" class="form-control" placeholder="Es. ecopelle, metallo">' in html
    assert '<input type="text" id="mob-art-materials" class="form-control" placeholder="Es. ecopelle, metallo" required>' not in html

    # 2. JS check: warehouse.js does not block submission when materials is empty
    res_js = client.get("/static/js/warehouse.js")
    assert res_js.status_code == 200
    js = res_js.text
    assert "Inserisci almeno un materiale." not in js

    # 3. API check: patch blueprint with empty materials list succeeds
    from src.models.pim import Brand, ArticleBlueprint
    brand = Brand(name="TestBrandMaterials")
    db_session.add(brand)
    db_session.commit()

    bp = ArticleBlueprint(
        brand_id=brand.id,
        article_name="Test Blueprint",
        description="Short desc",
        extended_description="Ext desc",
        tags=["tag1"],
        materials=["pelle"]
    )
    db_session.add(bp)
    db_session.commit()

    res_patch = client.patch(
        f"/api/v1/catalog/{bp.id}",
        json={"materials": []}
    )
    assert res_patch.status_code == 200
    assert res_patch.json()["status"] == "success"
    db_session.refresh(bp)
    assert bp.materials == []


def test_article_color_editing_in_warehouse_forms_and_api(db_session):
    """
    Verify that article color can be edited in both desktop modal and mobile panel:
    1. index.html contains #edit-article-color in desktop modal.
    2. index.html contains #mob-art-color in mobile panel.
    3. warehouse.js populates and reads color fields, sending new_color in patchCatalogItem payload.
    """
    client = TestClient(app)

    # 1 & 2: Template checks
    res = client.get("/magazzino")
    assert res.status_code == 200
    html = res.text

    assert 'id="edit-article-color"' in html
    assert 'for="edit-article-color"' in html
    assert 'id="mob-art-color"' in html
    assert 'for="mob-art-color"' in html

    # 3: JS checks
    res_js = client.get("/static/js/warehouse.js")
    assert res_js.status_code == 200
    js = res_js.text

    assert 'edit-article-color' in js
    assert 'mob-art-color' in js
    assert 'new_color:' in js


def test_warehouse_search_toolbar_ui_layout_and_responsive_styles(db_session):
    """
    Verify the cleaned-up warehouse toolbar layout and styling for desktop/tablet and mobile:
    1. DOM Structure: .search-toolbar contains .search-toolbar-main and .search-toolbar-sub.
    2. Desktop/Tablet header cleanup: barcode button is hidden via CSS, stock toggle and sort
       dropdown are moved under the search bar into .search-toolbar-sub.
    3. Mobile layout: barcode button is placed to the left of the search bar, sort dropdown
       is placed under the search bar on the left.
    4. De-emphasized styles: sort dropdown and stock toggle have border: none, background: transparent,
       font-size 12px or 11px, and darker/muted text color (--text-muted).
    """
    client = TestClient(app)
    res = client.get("/magazzino")
    assert res.status_code == 200
    html = res.text

    # 1. Structure: main toolbar and sub-bar
    assert 'search-toolbar-main' in html
    assert 'search-toolbar-sub' in html

    # 2. Barcode scan button positioned before search input wrapper
    idx_scan_btn = html.find('id="warehouse-barcode-scan-btn"')
    idx_search_input = html.find('id="warehouse-search-input"')
    assert idx_scan_btn != -1 and idx_search_input != -1
    assert idx_scan_btn < idx_search_input, "Barcode scan button must be to the left of the search input"

    # 3. Sort dropdown and stock toggle placed inside .search-toolbar-sub
    idx_sub = html.find('class="search-toolbar-sub"')
    idx_sort = html.find('id="warehouse-sort-select"')
    idx_toggle = html.find('id="warehouse-stock-toggle"')
    assert idx_sub != -1 and idx_sort != -1 and idx_toggle != -1
    assert idx_sub < idx_sort < idx_toggle, "Sort dropdown should be in sub-toolbar on the left, followed by stock toggle"

    # 4. CSS: Desktop hides scan button, mobile displays it on left
    assert ".search-scan-btn {\n    display: none !important;\n  }" in html
    assert "display: inline-flex !important;" in html

    # 5. CSS: De-emphasized borderless styling with --text-muted
    assert ".warehouse-sort-wrapper" in html
    assert "background: transparent !important;" in html
    assert "color: var(--text-muted);" in html











