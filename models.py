import os
import time
from sqlalchemy import *
from sqlalchemy.exc import OperationalError

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///alerts.db")

Railway gives postgres:// but SQLAlchemy needs postgresql://

if DATABASE_URL.startswith("postgres://"):
DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(
DATABASE_URL,
pool_pre_ping=True,
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

def init_db():
for attempt in range(12):
try:
metadata.create_all(engine)
print("Database initialized successfully")
return
except OperationalError as e:
print(
f"Database not ready ({attempt + 1}/12). Retrying in 5 seconds..."
)
time.sleep(5)

raise RuntimeError("Could not connect to database after 60 seconds")