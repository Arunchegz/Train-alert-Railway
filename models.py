import os
from sqlalchemy import *

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///alerts.db")

# Railway gives postgres:// but SQLAlchemy needs postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)
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
)

metadata.create_all(engine)
