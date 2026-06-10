import os
import time
from sqlalchemy import *

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///alerts.db")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace(
        "postgres://",
        "postgresql://",
        1
    )

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True
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
)

# Retry DB startup
for attempt in range(15):
    try:
        metadata.create_all(engine)
        print("Database connected")
        break
    except Exception as e:
        print(f"Database not ready ({attempt+1}/15): {e}")
        time.sleep(5)