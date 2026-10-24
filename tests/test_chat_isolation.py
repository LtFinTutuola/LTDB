"""
tests/test_chat_isolation.py
----------------------------
Verifies that SalesChatSessions are strictly isolated per user and that
is_active flags do not interfere between different users.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from src.main import app
from src.core.config import settings
from src.core.security import hash_password, create_access_token
from src.core.auth import get_current_user, bootstrap_auth
from src.models.users import User, UserRole
from src.models.sales import SalesChatSession
from src.repositories.sales_repo import sales_chat_repo


def test_chat_session_user_isolation(db_session: Session):
    """Verifies that each user only sees their own chat sessions and cannot access others'."""
    app.dependency_overrides.pop(get_current_user, None)

    user_a = User(
        username="user_a",
        hashed_password=hash_password("PassA123"),
        role=UserRole.BASE.value,
        is_active=True,
        token_version=1,
    )
    user_b = User(
        username="user_b",
        hashed_password=hash_password("PassB123"),
        role=UserRole.BASE.value,
        is_active=True,
        token_version=1,
    )
    db_session.add_all([user_a, user_b])
    db_session.commit()

    token_a = create_access_token(user_a.id, user_a.username, user_a.role, 1)
    token_b = create_access_token(user_b.id, user_b.username, user_b.role, 1)

    client_a = TestClient(app)
    client_a.cookies.set(settings.AUTH_COOKIE_NAME, token_a)

    client_b = TestClient(app)
    client_b.cookies.set(settings.AUTH_COOKIE_NAME, token_b)

    # User A creates a session directly in repo
    session_a = sales_chat_repo.create_session(db_session, title="Sessione di A", user_id=user_a.id)
    # User B creates a session directly in repo
    session_b = sales_chat_repo.create_session(db_session, title="Sessione di B", user_id=user_b.id)

    # 1. User A lists chats -> sees only Session A
    res_a = client_a.get("/api/v1/sales/chats")
    assert res_a.status_code == 200
    ids_a = [s["id"] for s in res_a.json()]
    assert str(session_a.id) in ids_a
    assert str(session_b.id) not in ids_a

    # 2. User B lists chats -> sees only Session B
    res_b = client_b.get("/api/v1/sales/chats")
    assert res_b.status_code == 200
    ids_b = [s["id"] for s in res_b.json()]
    assert str(session_b.id) in ids_b
    assert str(session_a.id) not in ids_b

    # 3. User A attempts to view User B's session detail -> returns 404
    res_detail = client_a.get(f"/api/v1/sales/chats/{session_b.id}")
    assert res_detail.status_code == 404


def test_independent_is_active_flags(db_session: Session):
    """Verifies that activating a session for User A does NOT deactivate User B's active session."""
    user_a = User(
        username="worker_1",
        hashed_password=hash_password("Pass123"),
        role=UserRole.BASE.value,
        is_active=True,
        token_version=1,
    )
    user_b = User(
        username="worker_2",
        hashed_password=hash_password("Pass123"),
        role=UserRole.BASE.value,
        is_active=True,
        token_version=1,
    )
    db_session.add_all([user_a, user_b])
    db_session.commit()

    # User A has an active session
    sess_a1 = sales_chat_repo.create_session(db_session, title="A1", user_id=user_a.id, is_active=True)
    # User B has an active session
    sess_b1 = sales_chat_repo.create_session(db_session, title="B1", user_id=user_b.id, is_active=True)

    # User A creates a second session (A2) that becomes active
    sess_a2 = sales_chat_repo.create_session(db_session, title="A2", user_id=user_a.id, is_active=True)

    db_session.refresh(sess_a1)
    db_session.refresh(sess_a2)
    db_session.refresh(sess_b1)

    # A1 should be deactivated, A2 should be active
    assert sess_a1.is_active is False
    assert sess_a2.is_active is True

    # Crucial check: User B's session (B1) MUST remain active!
    assert sess_b1.is_active is True


def test_bootstrap_backfills_legacy_chat_sessions(db_session: Session):
    """Verifies that bootstrap_auth assigns orphan chat sessions to the admin user."""
    from sqlalchemy import text
    # Create legacy session with NULL user_id using raw SQL
    db_session.execute(
        text("INSERT INTO sales_chat_sessions (id, title, is_active, user_id) VALUES ('legacy-sess-1', 'Chat Antica', 1, NULL)")
    )
    db_session.commit()

    orphan = db_session.query(SalesChatSession).filter(SalesChatSession.id == "legacy-sess-1").first()
    assert orphan.user_id is None

    # Run bootstrap
    bootstrap_auth(db_session)

    db_session.refresh(orphan)
    assert orphan.user_id is not None

    admin_user = db_session.query(User).filter(User.role == UserRole.ADMIN.value).first()
    assert orphan.user_id == admin_user.id
