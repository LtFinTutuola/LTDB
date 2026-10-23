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
from src.models import pim, wms, sales, staging, excel_sales  # noqa: F401

# Register the test logger plugin hooks
from tests.test_logger_plugin import pytest_runtest_makereport, pytest_sessionfinish  # noqa: F401


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
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)
    engine.dispose()
