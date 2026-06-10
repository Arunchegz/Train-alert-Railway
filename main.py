import logging
import os
from datetime import datetime, date

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import insert, select, update, delete
from telegram import Update

from models import engine, alerts, init_db
from railway import get_status
from telegram_bot import send_alert, build_application, BOT_TOKEN, WEBHOOK_URL
from scheduler import scheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("train-alert")

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Single shared bot instance
_bot_app = build_application()


# ── Alert expiry ──────────────────────────────────────────────────────────────
def purge_expired_alerts():
    """Delete alerts whose journey date has already passed."""
    today = date.today()
    expired = []

    with engine.connect() as conn:
        rows = conn.execute(select(alerts)).fetchall()

    for row in rows:
        try:
            journey = datetime.strptime(row.journey_date, "%d-%m-%Y").date()
            if journey < today:
                expired.append(row.id)
        except ValueError:
            logger.warning(f"Could not parse date for alert {row.id}: {row.journey_date}")

    if expired:
        with engine.connect() as conn:
            conn.execute(delete(alerts).where(alerts.c.id.in_(expired)))
            conn.commit()
        logger.info(f"Purged {len(expired)} expired alert(s): IDs {expired}")
    else:
        logger.info("No expired alerts to purge")


# ── Alert checker ─────────────────────────────────────────────────────────────
def check_alerts():
    logger.info("=" * 50)
    logger.info(f"Checking alerts at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 50)

    purge_expired_alerts()

    with engine.connect() as conn:
        rows = conn.execute(select(alerts)).fetchall()

    logger.info(f"Found {len(rows)} active alerts")

    for row in rows:
        logger.info(f"Checking alert ID {row.id}")
        if row.notified:
            logger.info("Already notified, skipping")
            continue

        try:
            status = get_status(
                row.train_number,
                row.from_station,
                row.to_station,
                row.journey_date,
                row.class_code,
            )
            logger.info(f"Train={row.train_number} Class={row.class_code} Status={status}")

            status = str(status).strip().upper()
            if status not in [
                "REGRET",
                "NOT AVAILABLE",
                "TRAIN DEPARTED",
                "NOT FOUND",
                "TIMEOUT",
                "ERROR",
            ]:
                logger.info("Bookable status found, sending Telegram alert")
                send_alert(
                    row.telegram_chat_id,
                    f"🚆 Train Booking Alert!\n\n"
                    f"Train: {row.train_number}\n"
                    f"Route: {row.from_station} → {row.to_station}\n"
                    f"Class: {row.class_code}\n"
                    f"Status: {status}\n",
                )
                with engine.connect() as conn:
                    conn.execute(
                        update(alerts)
                        .where(alerts.c.id == row.id)
                        .values(notified=True)
                    )
                    conn.commit()
                logger.info(f"Notification sent for alert {row.id}")

        except Exception as e:
            logger.exception(f"Error checking alert {row.id}: {e}")


# ── FastAPI lifecycle ─────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup_event():
    # Wait for PostgreSQL to be ready
    init_db()

    await _bot_app.initialize()
    await _bot_app.start()

    if WEBHOOK_URL:
        await _bot_app.bot.set_webhook(
            url=f"{WEBHOOK_URL}/webhook",
            drop_pending_updates=True,
        )
        logger.info(f"Webhook set → {WEBHOOK_URL}/webhook")
    else:
        logger.warning("WEBHOOK_URL not set — bot will not receive messages!")

    scheduler.add_job(
        check_alerts,
        "interval",
        minutes=10,
        id="train_alert_checker",
        replace_existing=True,
    )
    scheduler.add_job(
        purge_expired_alerts,
        "cron",
        hour=0,
        minute=0,
        id="purge_expired_alerts",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started")


@app.on_event("shutdown")
async def shutdown_event():
    await _bot_app.stop()
    await _bot_app.shutdown()
    scheduler.shutdown()
    logger.info("Shutdown complete")


# ── Webhook endpoint ──────────────────────────────────────────────────────────
@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    tg_update = Update.de_json(data, _bot_app.bot)
    await _bot_app.process_update(tg_update)
    return JSONResponse(content={"ok": True})


# ── Web routes ────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.post("/add-alert")
def add_alert_web(
    train_number: str = Form(...),
    from_station: str = Form(...),
    to_station: str = Form(...),
    journey_date: str = Form(...),
    class_code: str = Form(...),
    telegram_chat_id: str = Form(...),
):
    with engine.connect() as conn:
        conn.execute(
            insert(alerts).values(
                train_number=train_number,
                from_station=from_station,
                to_station=to_station,
                journey_date=journey_date,
                class_code=class_code,
                telegram_chat_id=telegram_chat_id,
                notified=False,
            )
        )
        conn.commit()
    return {"success": True, "message": "Alert saved"}


@app.get("/test-check")
def test_check():
    check_alerts()
    return {"success": True, "message": "Manual check completed"}
