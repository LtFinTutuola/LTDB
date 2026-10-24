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

    # 2. Base user CANNOT access Settings API or HTML page
    res_settings_api = base_client.get("/api/v1/settings")
    assert res_settings_api.status_code == 403

    res_settings_html = base_client.get("/settings")
    assert res_settings_html.status_code == 403

    # 3. Base user CANNOT access Users IAM API
    res_users_api = base_client.get("/api/v1/users")
    assert res_users_api.status_code == 403

    # 4. Admin CAN access all of the above
    assert admin_client.get("/api/v1/settings").status_code == 200
    assert admin_client.get("/settings").status_code == 200
    assert admin_client.get("/api/v1/users").status_code == 200


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
