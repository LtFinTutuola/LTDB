from typing import Any
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from src.core.config import settings

# Create sync engine
engine = create_engine(
    settings.SQLITE_URL,
    echo=False,  # Set to True for debugging SQL queries
    connect_args={"check_same_thread": False}, # Needed for SQLite with FastAPI
)

# Enforce WAL mode and NORMAL synchronous on connection for performance and concurrency
@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection: Any, connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    cursor.execute("PRAGMA foreign_keys=ON;") # Ensure foreign key constraints are enforced
    cursor.close()

# Create SessionLocal factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    """
    Dependency to get a DB session.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
