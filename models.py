import os
from sqlalchemy import (
    create_engine,
    MetaData,
    Table,
    Column,
    Integer,
    String,
    Boolean,
    text,
)

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///alerts.db")

# Railway gives postgres:// but SQLAlchemy needs postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False}
    if DATABASE_URL.startswith("sqlite")
    else {},
)

metadata = MetaData()

alerts = Table(
    "alerts",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("train_number", String),
    Column("from_station", String),
    Column("to_station", String),
    Column("journey_date", String),
    Column("class_code", String),
    Column("telegram_chat_id", String),
    Column("notified", Boolean, default=False),
    Column("is_buzzing", Boolean, default=False),
    Column("alert_on", String, default="AVAILABLE"),
    Column("last_checked_status", String, nullable=True),
    Column("last_checked_time", String, nullable=True),
)


def init_db():
    """Create tables if they don't exist and run auto-migrations."""
    metadata.create_all(engine)

    with engine.connect() as conn:
        try:
            if DATABASE_URL.startswith("sqlite"):
                cursor = conn.execute(text("PRAGMA table_info(alerts)"))
                columns = [row[1] for row in cursor.fetchall()]
            else:
                cursor = conn.execute(text(
                    "SELECT column_name FROM information_schema.columns WHERE table_name='alerts'"
                ))
                columns = [row[0] for row in cursor.fetchall()]

            if "is_buzzing" not in columns:
                conn.execute(text("ALTER TABLE alerts ADD COLUMN is_buzzing BOOLEAN DEFAULT 0"))
                print("Database migration: Added is_buzzing column")
            if "alert_on" not in columns:
                conn.execute(text("ALTER TABLE alerts ADD COLUMN alert_on VARCHAR DEFAULT 'AVAILABLE'"))
                print("Database migration: Added alert_on column")
            if "last_checked_status" not in columns:
                conn.execute(text("ALTER TABLE alerts ADD COLUMN last_checked_status VARCHAR"))
                print("Database migration: Added last_checked_status column")
            if "last_checked_time" not in columns:
                conn.execute(text("ALTER TABLE alerts ADD COLUMN last_checked_time VARCHAR"))
                print("Database migration: Added last_checked_time column")

            conn.commit()
        except Exception as e:
            print(f"Migration check/execution error: {e}")
