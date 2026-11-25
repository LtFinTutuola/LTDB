"""
tests/test_auth.py
------------------
Comprehensive test suite for the LTDB authentication, JWT token versioning,
RBAC authorization (Base vs Admin), edge route bypass, and user management.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from src.main import app
from src.core.config import settings
from src.core.security import hash_password, create_access_token
from src.core.auth import get_current_user
from src.models.users import User, UserRole


def test_login_success_and_cookie(db_session: Session):
    """Verifies that valid credentials return a 200 and set the HttpOnly cookie."""
    app.dependency_overrides.pop(get_current_user, None)
    user = User(
        username="mario",
        hashed_password=hash_password("SuperSecret123"),
        role=UserRole.BASE.value,
        is_active=True,
        token_version=1,
    )
    db_session.add(user)
    db_session.commit()

    client = TestClient(app)
    res = client.post("/api/v1/auth/login", json={"username": "mario", "password": "SuperSecret123"})
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert data["user"]["username"] == "mario"
    assert data["user"]["role"] == "base"
    assert settings.AUTH_COOKIE_NAME in client.cookies


def test_login_invalid_credentials(db_session: Session):
    """Verifies that invalid credentials or inactive account return a 401."""
    app.dependency_overrides.pop(get_current_user, None)
    user = User(
        username="luigi",
        hashed_password=hash_password("Password123"),
        role=UserRole.BASE.value,
        is_active=False,  # Inactive
        token_version=1,
    )
    db_session.add(user)
    db_session.commit()

    client = TestClient(app)
    # Wrong password
    res = client.post("/api/v1/auth/login", json={"username": "luigi", "password": "WrongPassword"})
    assert res.status_code == 401

    # Inactive user with right password
    res = client.post("/api/v1/auth/login", json={"username": "luigi", "password": "Password123"})
    assert res.status_code == 401

    # Non-existent user
    res = client.post("/api/v1/auth/login", json={"username": "ghost", "password": "AnyPassword"})
    assert res.status_code == 401


def test_unauthenticated_api_and_html_redirect(db_session: Session):
    """Verifies that API calls return 401 while HTML calls redirect to /login."""
    app.dependency_overrides.pop(get_current_user, None)
    client = TestClient(app)

    # API call without token
    api_res = client.get("/api/v1/catalog/items")
    assert api_res.status_code == 401
    assert api_res.json()["detail"] == "Non autenticato"

    # HTML page request without token
    html_res = client.get("/vendite", follow_redirects=False)
    assert html_res.status_code == 303
    assert html_res.headers["location"].startswith("/login?next=/vendite")


def test_token_version_invalidation(db_session: Session):
    """Verifies that changing a user's password or token_version invalidates previous JWTs."""
    app.dependency_overrides.pop(get_current_user, None)
    user = User(
        username="gianni",
        hashed_password=hash_password("VecchiaPassword123"),
        role=UserRole.BASE.value,
        is_active=True,
        token_version=1,
    )
    db_session.add(user)
    db_session.commit()

    # Generate token for version 1
    token_v1 = create_access_token(user.id, user.username, user.role, token_version=1)
    client = TestClient(app)
    client.cookies.set(settings.AUTH_COOKIE_NAME, token_v1)

    # Access works with token v1
    res = client.get("/api/v1/auth/me")
    assert res.status_code == 200
    assert res.json()["username"] == "gianni"

    # Invalidate session by incrementing token_version
    user.token_version = 2
    db_session.commit()

    # Now token v1 is rejected
    res_rejected = client.get("/api/v1/auth/me")
    assert res_rejected.status_code == 401
    assert "Sessione revocata" in res_rejected.json()["detail"]


def test_change_password_endpoint(db_session: Session):
    """Verifies that an authenticated user can change their own password."""
    app.dependency_overrides.pop(get_current_user, None)
    user = User(
        username="elena",
        hashed_password=hash_password("PasswordVecchia1"),
        role=UserRole.BASE.value,
        is_active=True,
        token_version=1,
    )
    db_session.add(user)
    db_session.commit()

    client = TestClient(app)
    # Login
    login_res = client.post("/api/v1/auth/login", json={"username": "elena", "password": "PasswordVecchia1"})
    assert login_res.status_code == 200

    # Change password
    cp_res = client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "PasswordVecchia1", "new_password": "PasswordNuova2"},
    )
    assert cp_res.status_code == 200

    # User in DB now has token_version=2
    db_session.refresh(user)
    assert user.token_version == 2

    # New cookie set on client still allows access
    me_res = client.get("/api/v1/auth/me")
    assert me_res.status_code == 200


def test_rbac_restrictions_base_vs_admin(db_session: Session):
    """
    Verifies that a Base user can access brands, categories, and warehouse,
    but is strictly forbidden from /api/v1/settings, /settings, and /api/v1/users.
    """
    app.dependency_overrides.pop(get_current_user, None)
    base_user = User(
        username="base_user",
        hashed_password=hash_password("PassBase123"),
        role=UserRole.BASE.value,
        is_active=True,
        token_version=1,
    )
    admin_user = User(
        username="admin_user",
        hashed_password=hash_password("PassAdmin123"),
        role=UserRole.ADMIN.value,
        is_active=True,
        token_version=1,
    )
    db_session.add_all([base_user, admin_user])
    db_session.commit()

    base_token = create_access_token(base_user.id, base_user.username, base_user.role, 1)
    admin_token = create_access_token(admin_user.id, admin_user.username, admin_user.role, 1)

    base_client = TestClient(app)
    base_client.cookies.set(settings.AUTH_COOKIE_NAME, base_token)

    admin_client = TestClient(app)
    admin_client.cookies.set(settings.AUTH_COOKIE_NAME, admin_token)

    # 1. Base user can access Brands and Categories (APIs and HTML pages)
    assert base_client.get("/api/v1/catalog/brands").status_code == 200
    assert base_client.get("/api/v1/catalog/categories").status_code == 200
    assert base_client.get("/brands").status_code == 200
    assert base_client.get("/categories").status_code == 200

    # 2. Base user CANNOT access Settings API (admin or excel), Force Sync, or HTML page
    res_settings_api = base_client.get("/api/v1/settings")
    assert res_settings_api.status_code == 403
    assert base_client.get("/api/v1/settings/excel").status_code == 403
    assert base_client.post("/api/v1/excel-synchronization/force-sync").status_code == 403

    res_settings_html = base_client.get("/settings")
    assert res_settings_html.status_code == 403

    # 3. Base user CANNOT access Users IAM API
    res_users_api = base_client.get("/api/v1/users")
    assert res_users_api.status_code == 403

    # 4. Admin CAN access all of the above
    assert admin_client.get("/api/v1/settings").status_code == 200
    assert admin_client.get("/settings").status_code == 200
    assert admin_client.get("/api/v1/users").status_code == 200


def test_rbac_cassa_role_access_and_admin_restrictions(db_session: Session):
    """
    Verifies the intermediate 'cassa' role:
    - Can access operational endpoints and HTML pages (brands, categories, sidebar shows '(cassa)' AND '/settings').
    - Can access '/settings' HTML (seeing ONLY the 'File Excel' tab, without System/Gemini or Users tabs),
      '/api/v1/settings/excel', and '/api/v1/excel-synchronization/force-sync'.
    - Is strictly forbidden (403) from admin-only '/api/v1/settings' and all '/api/v1/users' IAM endpoints.
    """
    app.dependency_overrides.pop(get_current_user, None)
    cassa_user = User(
        username="cassa_operatore",
        hashed_password=hash_password("PassCassa123"),
        role=UserRole.CASSA.value,
        is_active=True,
        token_version=1,
    )
    admin_user = User(
        username="admin_cassa_test",
        hashed_password=hash_password("PassAdmin123"),
        role=UserRole.ADMIN.value,
        is_active=True,
        token_version=1,
    )
    db_session.add_all([cassa_user, admin_user])
    db_session.commit()

    cassa_token = create_access_token(cassa_user.id, cassa_user.username, cassa_user.role, 1)
    cassa_client = TestClient(app)
    cassa_client.cookies.set(settings.AUTH_COOKIE_NAME, cassa_token)

    # 1. Cassa user can access operational APIs and HTML pages, and sees Settings link in sidebar
    assert cassa_client.get("/api/v1/catalog/brands").status_code == 200
    assert cassa_client.get("/api/v1/catalog/categories").status_code == 200
    brands_html = cassa_client.get("/brands")
    assert brands_html.status_code == 200
    assert "cassa_operatore (cassa)" in brands_html.text
    assert 'href="/settings"' in brands_html.text

    # 2. Cassa user CAN access /settings HTML (seeing ONLY the File Excel tab) and Excel Settings / Force Sync APIs
    cassa_settings_html = cassa_client.get("/settings")
    assert cassa_settings_html.status_code == 200
    assert 'id="tab-btn-excel"' in cassa_settings_html.text
    assert 'id="excel-settings-section"' in cassa_settings_html.text
    assert 'id="tab-btn-config"' not in cassa_settings_html.text
    assert 'id="tab-btn-users"' not in cassa_settings_html.text
    assert "Password Gemini" not in cassa_settings_html.text
    assert "Crea Nuovo Utente" not in cassa_settings_html.text

    assert cassa_client.get("/api/v1/settings/excel").status_code == 200
    assert cassa_client.post("/api/v1/excel-synchronization/force-sync").status_code == 200

    # 3. Cassa user CANNOT access Admin System/Gemini Settings API -> 403 Forbidden
    assert cassa_client.get("/api/v1/settings").status_code == 403
    assert (
        cassa_client.put(
            "/api/v1/settings",
            json={
                "db_connection_string": "sqlite:///hack.db",
                "api_key": "k",
                "codes_deduction_agent_model": "m",
            },
        ).status_code
        == 403
    )

    # 3. Cassa user CANNOT access any Users IAM endpoint -> 403 Forbidden
    assert cassa_client.get("/api/v1/users").status_code == 403
    assert (
        cassa_client.post(
            "/api/v1/users",
            json={"username": "nuovo", "password": "Password123", "role": "base"},
        ).status_code
        == 403
    )
    assert (
        cassa_client.patch(
            f"/api/v1/users/{cassa_user.id}",
            json={"role": "admin"},
        ).status_code
        == 403
    )
    assert (
        cassa_client.put(
            f"/api/v1/users/{cassa_user.id}/password",
            json={"new_password": "Password123"},
        ).status_code
        == 403
    )
    assert cassa_client.post("/api/v1/users/revoke-all").status_code == 403


def test_cassa_role_creation_promotion_and_last_admin_guard(db_session: Session):
    """
    Verifies admin can create and promote users to 'cassa', settings UI exposes 'cassa',
    and the sole active admin cannot be demoted to 'cassa'.
    """
    app.dependency_overrides.pop(get_current_user, None)
    admin = User(
        username="root_admin_cassa",
        hashed_password=hash_password("RootAdmin123"),
        role=UserRole.ADMIN.value,
        is_active=True,
        token_version=1,
    )
    db_session.add(admin)
    db_session.commit()

    admin_token = create_access_token(admin.id, admin.username, admin.role, 1)
    client = TestClient(app)
    client.cookies.set(settings.AUTH_COOKIE_NAME, admin_token)

    # 1. Create user directly with role='cassa'
    res_cassa = client.post(
        "/api/v1/users",
        json={"username": "operatore_cassa", "password": "Password123", "role": "cassa"},
    )
    assert res_cassa.status_code == 201
    assert res_cassa.json()["role"] == "cassa"

    # 2. Create a 'base' user and promote to 'cassa'
    res_base = client.post(
        "/api/v1/users",
        json={"username": "operatore_base", "password": "Password123", "role": "base"},
    )
    assert res_base.status_code == 201
    base_id = res_base.json()["id"]

    res_promote = client.patch(
        f"/api/v1/users/{base_id}",
        json={"role": "cassa"},
    )
    assert res_promote.status_code == 200
    assert res_promote.json()["role"] == "cassa"

    # 3. Settings page HTML & JS expose 'cassa' role option
    settings_html = client.get("/settings")
    assert settings_html.status_code == 200
    assert '<option value="cassa">Cassa</option>' in settings_html.text

    settings_js = client.get("/static/js/settings_page.js")
    assert settings_js.status_code == 200
    assert '<option value="cassa"' in settings_js.text

    # 4. Guard test: sole active admin cannot be demoted to 'cassa'
    db_session.query(User).filter(User.id != admin.id).delete()
    db_session.commit()

    demote_cassa = client.patch(
        f"/api/v1/users/{admin.id}",
        json={"role": "cassa"},
    )
    assert demote_cassa.status_code == 400
    assert "impossibile disattivare o declassare" in demote_cassa.json()["detail"].lower()


def test_edge_agent_public_bypass(db_session: Session):
    """Verifies that cash register synchronization endpoints do not require auth."""
    app.dependency_overrides.pop(get_current_user, None)
    client = TestClient(app)

    # GET /agent-config without any auth cookie
    res_config = client.get("/api/v1/excel-synchronization/agent-config")
    assert res_config.status_code == 200
    assert res_config.json()["status"] == "success"

    # POST /push-batch without any auth cookie
    res_push = client.post(
        "/api/v1/excel-synchronization/push-batch",
        json={"rows": []},
    )
    assert res_push.status_code == 200
    assert res_push.json()["status"] == "success"


def test_admin_user_crud_and_guards(db_session: Session):
    """Verifies user creation, password reset, and protection of the last admin."""
    app.dependency_overrides.pop(get_current_user, None)
    admin = User(
        username="root_admin",
        hashed_password=hash_password("RootAdmin123"),
        role=UserRole.ADMIN.value,
        is_active=True,
        token_version=1,
    )
    db_session.add(admin)
    db_session.commit()

    admin_token = create_access_token(admin.id, admin.username, admin.role, 1)
    client = TestClient(app)
    client.cookies.set(settings.AUTH_COOKIE_NAME, admin_token)

    # 1. Create new user
    create_res = client.post(
        "/api/v1/users",
        json={"username": "cassiere1", "password": "Password123", "role": "base"},
    )
    assert create_res.status_code == 201
    created_id = create_res.json()["id"]

    # 2. Reset password for created user
    reset_res = client.put(
        f"/api/v1/users/{created_id}/password",
        json={"new_password": "NuovaPassword123"},
    )
    assert reset_res.status_code == 200

    # 3. Guard test: attempt to demote the sole active admin
    # Remove any other mock admins so only 'root_admin' is the active admin
    db_session.query(User).filter(User.id != admin.id).delete()
    db_session.commit()

    demote_res = client.patch(
        f"/api/v1/users/{admin.id}",
        json={"role": "base"},
    )
    assert demote_res.status_code == 400
    assert "impossibile disattivare o declassare" in demote_res.json()["detail"].lower()

    # 4. Hard reset: revoke all sessions
    revoke_res = client.post("/api/v1/users/revoke-all")
    assert revoke_res.status_code == 200
    db_session.refresh(admin)
    assert admin.token_version == 2


def test_route_guard_whitelist():
    """
    Verifies that every API endpoint in the application requires authentication
    except the explicitly defined public whitelist.
    """
    public_whitelist = {
        "/api/v1/auth/login",
        "/api/v1/excel-synchronization/agent-config",
        "/api/v1/excel-synchronization/push-batch",
        "/openapi.json",
        "/docs",
        "/docs/oauth2-redirect",
    }

    client = TestClient(app)
    app.dependency_overrides.pop(get_current_user, None)

    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", set())
        if not path or not path.startswith("/api/"):
            continue

        if path in public_whitelist:
            continue

        for method in methods:
            if method in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                response = client.request(method, path)
                assert response.status_code in {401, 403, 303}, (
                    f"Rotta non protetta rilevata: {method} {path} ha risposto con {response.status_code}"
                )


def test_password_length_policy_4_chars(db_session: Session):
    """Verifies that the minimum password length policy is exactly 4 characters across all auth endpoints."""
    app.dependency_overrides.pop(get_current_user, None)
    admin = User(
        username="policy_admin",
        hashed_password=hash_password("admin123"),
        role=UserRole.ADMIN.value,
        is_active=True,
        token_version=1,
    )
    db_session.add(admin)
    db_session.commit()

    admin_token = create_access_token(admin.id, admin.username, admin.role, 1)
    client = TestClient(app)
    client.cookies.set(settings.AUTH_COOKIE_NAME, admin_token)

    # 1. User creation with 4-char password -> 201 Created
    res_create_4 = client.post(
        "/api/v1/users",
        json={"username": "user4chars", "password": "abcd", "role": "base"},
    )
    assert res_create_4.status_code == 201
    created_id = res_create_4.json()["id"]

    # 2. User creation with 3-char password -> 422 Unprocessable Entity
    res_create_3 = client.post(
        "/api/v1/users",
        json={"username": "user3chars", "password": "abc", "role": "base"},
    )
    assert res_create_3.status_code == 422

    # 2b. User creation with 4-char username and 4-char password (e.g. luisa/luisa and luis/luis) -> 201 Created
    res_luisa = client.post(
        "/api/v1/users",
        json={"username": "luisa", "password": "luisa", "role": "base"},
    )
    assert res_luisa.status_code == 201

    res_user_4 = client.post(
        "/api/v1/users",
        json={"username": "luis", "password": "luis", "role": "base"},
    )
    assert res_user_4.status_code == 201

    # 2c. User creation with 3-char username -> 422 Unprocessable Entity
    res_user_3 = client.post(
        "/api/v1/users",
        json={"username": "lui", "password": "validpass", "role": "base"},
    )
    assert res_user_3.status_code == 422

    # 3. Admin reset password with 4-char password -> 200 OK
    res_reset_4 = client.put(
        f"/api/v1/users/{created_id}/password",
        json={"new_password": "1234"},
    )
    assert res_reset_4.status_code == 200

    # 4. Admin reset password with 3-char password -> 422 Unprocessable Entity
    res_reset_3 = client.put(
        f"/api/v1/users/{created_id}/password",
        json={"new_password": "123"},
    )
    assert res_reset_3.status_code == 422

    # 5. User self change-password with 4-char password -> 200 OK
    res_cp_4 = client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "admin123", "new_password": "pass"},
    )
    assert res_cp_4.status_code == 200

    # 6. User self change-password with 3-char password -> 422 Unprocessable Entity
    res_cp_3 = client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "pass", "new_password": "pas"},
    )
    assert res_cp_3.status_code == 422
