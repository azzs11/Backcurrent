"""
Database setup — engine, session factory, and init.

The db file lives at data/recovery.db. Path is resolved relative to the project
root so it doesn't matter what directory you run the app from.
"""

from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import Base

# resolve project root from this file's location — two levels up from db/session.py
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DB_PATH = _PROJECT_ROOT / "data" / "recovery.db"
DATABASE_URL = f"sqlite:///{_DB_PATH}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},  # SQLite blocks concurrent threads by default — FastAPI needs this off
    echo=False,  # set to True locally if you want to see the raw SQL while debugging
)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def init_db():
    """
    Creates all tables if they don't exist yet. Safe to call on every startup —
    SQLAlchemy's create_all is a no-op for tables that are already there.
    Also makes sure the data/ directory exists before SQLite tries to create the file.
    """
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)


@contextmanager
def get_db():
    """
    Context manager for getting a database session. Use this in scripts,
    background tasks, and anywhere outside FastAPI route handlers.

    Usage:
        with get_db() as db:
            db.query(PaymentAttempt).all()

    Commits on clean exit, rolls back on exception.
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db_dependency():
    """
    FastAPI dependency injection version. Wire this up with Depends() in routes.
    FastAPI calls it per request and the finally block closes the session after
    the response is sent — no need to manually close.

    Usage:
        @router.get("/something")
        def my_route(db: Session = Depends(get_db_dependency)):
            ...
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
