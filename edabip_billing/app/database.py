"""
Database wiring for the billing module.

INTEGRATION NOTE (read this first):
EDABIP's existing FastAPI backend almost certainly already has its own
`Base`, `engine`, `SessionLocal`, and `get_db()` in something like
`app/database.py` or `app/core/db.py`. You have two options:

  Option A (recommended): delete this file and, in every other file in
  this `billing` package, replace:
      from app.database import Base, get_db
  with the import path your existing project actually uses, e.g.:
      from app.db.session import Base, get_db

  Option B: keep this file as-is if the billing module lives in its own
  service/schema. `Base.metadata` below is self-contained, so
  `Base.metadata.create_all(engine)` or an Alembic autogenerate will only
  ever touch the `billing_*` tables defined in models.py.

Either way, nothing else in this package needs to change - every model
and route only ever imports `Base` and `get_db` from this one module.
"""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv(
    "BILLING_DATABASE_URL",
    os.getenv("DATABASE_URL", "mysql+pymysql://root:password@localhost:3306/edabip"),
)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
