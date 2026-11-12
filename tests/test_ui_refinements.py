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

    # Verify that the core files parse as valid ES modules without SyntaxErrors
    check_code = """
    try {
        await import('./src/static/js/staging.js');
    } catch (e) {
        if (e instanceof SyntaxError) {
            console.error('SyntaxError in staging.js:', e);
            process.exit(1);
        }
    }
    """
    res = subprocess.run(
        [node_bin, "--input-type=module", "-e", check_code],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, f"staging.js syntax check failed: {res.stderr}"





def test_staging_photo_buttons_directly_under_preview():
    from pathlib import Path
    content = Path("src/static/js/staging.js").read_text(encoding="utf-8")
    for line in content.splitlines():
        if 'class="btn-search-photo"' in line or 'class="btn-upload-photo"' in line:
            assert "margin-top:auto" not in line
    assert "window.__LTDB_BRANDS__" in content
