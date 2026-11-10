"""
tests/test_static_cache_control.py
-----------------------------------
Verifies that static assets (JavaScript and CSS) served under /static
are accompanied by 'Cache-Control: no-cache, must-revalidate' and 'Pragma: no-cache' headers.
This ensures browsers do not execute stale cached scripts after updates.
"""
from fastapi.testclient import TestClient
from src.main import app


def test_javascript_files_have_no_cache_headers(db_session):
    """
    Ensure that JavaScript files served under /static include cache revalidation headers.
    """
    client = TestClient(app)
    endpoints = [
        "/static/js/main.js",
        "/static/js/staging.js",
        "/static/js/warehouse.js",
        "/static/js/blueprint_revision.js",
        "/static/js/chat.js",
    ]

    for ep in endpoints:
        res = client.get(ep)
        assert res.status_code == 200, f"Failed to get {ep}"
        assert "no-cache" in res.headers.get("Cache-Control", "")
        assert "must-revalidate" in res.headers.get("Cache-Control", "")
        assert res.headers.get("Pragma") == "no-cache"


def test_css_files_have_no_cache_headers(db_session):
    """
    Ensure that CSS files served under /static include cache revalidation headers.
    """
    client = TestClient(app)
    endpoints = [
        "/static/css/app.css",
        "/static/css/tabs.css",
        "/static/css/sales_table.css",
        "/static/css/workspace.css",
    ]

    for ep in endpoints:
        res = client.get(ep)
        assert res.status_code == 200, f"Failed to get {ep}"
        assert "no-cache" in res.headers.get("Cache-Control", "")
        assert "must-revalidate" in res.headers.get("Cache-Control", "")
        assert res.headers.get("Pragma") == "no-cache"


def test_query_string_preserves_no_cache_headers(db_session):
    """
    Ensure that even when queried with query parameters (e.g. ?v=...),
    the response still includes Cache-Control: no-cache, must-revalidate.
    """
    client = TestClient(app)
    res = client.get("/static/js/main.js?v=test12345")
    assert res.status_code == 200
    assert "no-cache" in res.headers.get("Cache-Control", "")
    assert "must-revalidate" in res.headers.get("Cache-Control", "")
    assert res.headers.get("Pragma") == "no-cache"

