"""
Shared pytest fixtures for the LTDB test suite.

Uses an in-memory SQLite database so the real data/ltdb_data.db is never touched.
All tables are created fresh for every test function and torn down afterwards.
"""
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from src.models.base import Base
# Import all model modules so every table is registered on Base.metadata
from src.models import pim, wms, sales, staging, excel_sales, users  # noqa: F401

# Register the test logger plugin hooks
from tests.test_logger_plugin import pytest_runtest_makereport, pytest_sessionfinish  # noqa: F401
from src.core.request_context import AuthUser, current_user_var
from src.core.auth import get_current_user

FAKE_ADMIN = AuthUser(
    id="00000000-0000-0000-0000-000000000001",
    username="admin_test",
    role="admin",
    token_version=1,
)


@pytest.fixture(autouse=True)
def _auth_override():
    """
    Autouse fixture that overrides get_current_user with a mock admin user
    so that all existing tests continue to pass without 401 Unauthorized errors.
    """
    from src.main import app

    def override_user():
        current_user_var.set(FAKE_ADMIN)
        return FAKE_ADMIN

    app.dependency_overrides[get_current_user] = override_user
    current_user_var.set(FAKE_ADMIN)
    yield
    app.dependency_overrides.pop(get_current_user, None)
    current_user_var.set(None)


@pytest.fixture(scope="function")
def db_session():
    """
    Yields a clean SQLAlchemy Session backed by an in-memory SQLite database.
    Tables are created before and dropped after every test function.
    """
    from sqlalchemy.pool import StaticPool
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # Mirror the production PRAGMAs
    @event.listens_for(engine, "connect")
    def _set_pragma(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA synchronous=NORMAL;")
        cur.execute("PRAGMA foreign_keys=ON;")
        cur.close()

    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestSession()

    from src.models.users import User
    admin_user = User(
        id=FAKE_ADMIN.id,
        username=FAKE_ADMIN.username,
        hashed_password="fake_hashed_password",
        role=FAKE_ADMIN.role,
        is_active=True,
        token_version=FAKE_ADMIN.token_version,
    )
    session.add(admin_user)
    session.commit()

    from src.core.cache import search_snapshot_cache, query_plan_cache, color_decision_cache
    search_snapshot_cache.clear()
    query_plan_cache.clear()
    color_decision_cache.clear()
    
    from src.main import app
    from src.core.database import get_db
    
    def override_get_db():
        try:
            yield session
        finally:
            pass # Session is closed in the fixture
            
    app.dependency_overrides[get_db] = override_get_db

    yield session

    session.close()
    app.dependency_overrides.pop(get_db, None)
    Base.metadata.drop_all(bind=engine)
    engine.dispose()
