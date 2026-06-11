import logging
import os
import time
from datetime import datetime, date

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import insert, select, update, delete
from telegram import Update
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED

from models import engine, alerts, init_db
from railway import get_status
from telegram_bot import send_alert, send_buzz_message, stop_buzzer, active_buzzers, build_application, BOT_TOKEN, WEBHOOK_URL
from scheduler import scheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("train-alert")

ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")

app = FastAPI()
templates = Jinja2Templates(directory="templates")

_bot_app = build_application()

# ── Bad statuses that mean "no availability" ──────────────────────────────────
NO_AVAILABILITY_STATUSES = {
    "REGRET",
    "NOT AVAILABLE",
    "TRAIN DEPARTED",
    "NOT FOUND",
    "TIMEOUT",
    "ERROR",
}


# ── Scheduler crash listener ──────────────────────────────────────────────────
def on_scheduler_error(event):
    job_id = event.job_id
    exc = event.exception
    logger.error(f"Scheduler job '{job_id}' crashed: {exc}")
    if ADMIN_CHAT_ID:
        send_alert(
            ADMIN_CHAT_ID,
            f"⚠️ *Scheduler Job Crashed!*\n\n"
            f"Job: `{job_id}`\n"
            f"Error: `{exc}`\n\n"
            f"The job will resume at its next scheduled interval.",
        )
    else:
        logger.warning("ADMIN_CHAT_ID not set — cannot send crash notification")


def on_scheduler_missed(event):
    job_id = event.job_id
    scheduled_time = event.scheduled_run_time
    logger.warning(f"Scheduler job '{job_id}' missed its run at {scheduled_time}")
    if ADMIN_CHAT_ID:
        send_alert(
            ADMIN_CHAT_ID,
            f"⏰ *Scheduler Job Missed!*\n\n"
            f"Job: `{job_id}`\n"
            f"Was due at: `{scheduled_time}`\n\n"
            f"This usually means the server was overloaded or restarting.",
        )


# ── Alert expiry ──────────────────────────────────────────────────────────────
def purge_expired_alerts():
    """Delete alerts whose journey date has already passed."""
    today = date.today()
    expired = []

    try:
        with engine.connect() as conn:
            rows = conn.execute(select(alerts)).fetchall()
    except Exception as e:
        logger.error(f"DB unavailable during purge, skipping: {e}")
        return

    for row in rows:
        try:
            journey = datetime.strptime(row.journey_date, "%d-%m-%Y").date()
            if journey < today:
                expired.append(row.id)
        except ValueError:
            logger.warning(f"Could not parse date for alert {row.id}: {row.journey_date}")

    if expired:
        # Also stop any buzzers for expired alerts
        for aid in expired:
            active_buzzers.pop(aid, None)
        with engine.connect() as conn:
            conn.execute(delete(alerts).where(alerts.c.id.in_(expired)))
            conn.commit()
        logger.info(f"Purged {len(expired)} expired alert(s): IDs {expired}")
    else:
        logger.info("No expired alerts to purge")


# ── Buzz job (runs every 5 s per active alert) ────────────────────────────────
def buzz_alert(alert_id: int, chat_id: str, train_number: str,
               from_station: str, to_station: str, journey_date: str,
               class_code: str):
    """
    Called every 5 seconds while a seat is available.
    Re-checks availability; stops automatically if seats disappear.
    """
    if alert_id not in active_buzzers:
        # Buzzer was stopped externally (button press / delete) — remove the job
        try:
            scheduler.remove_job(f"buzz_{alert_id}")
        except Exception:
            pass
        return

    try:
        status = str(get_status(train_number, from_station, to_station, journey_date, class_code)).strip().upper()
    except Exception as e:
        logger.error(f"Buzz re-check error for alert {alert_id}: {e}")
        return

    if status in NO_AVAILABILITY_STATUSES:
        # Seats gone — stop buzzing, do NOT mark notified so it can re-trigger later
        logger.info(f"Seats gone for alert {alert_id}, stopping buzzer (status={status})")
        active_buzzers.pop(alert_id, None)
        try:
            scheduler.remove_job(f"buzz_{alert_id}")
        except Exception:
            pass
        send_alert(
            chat_id,
            f"😔 *Seats no longer available*\n\n"
            f"Train: {train_number} | {from_station} → {to_station}\n"
            f"Class: {class_code} | Status: {status}\n\n"
            f"I'll keep watching and alert you again if they open up.",
        )
        return

    # Seats still available — buzz again!
    logger.info(f"Buzzing alert {alert_id} (status={status})")
    send_buzz_message(
        chat_id,
        alert_id,
        f"🔔 *SEATS AVAILABLE — BOOK NOW!*\n\n"
        f"Train: *{train_number}*\n"
        f"Route: {from_station} → {to_station}\n"
        f"Date: {journey_date} | Class: {class_code}\n"
        f"Status: `{status}`\n\n"
        f"_Tap_ 🛑 *Stop Alert* _once you've booked._",
    )


# ── Alert checker ─────────────────────────────────────────────────────────────
def check_alerts():
    logger.info("=" * 50)
    logger.info(f"Checking alerts at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 50)

    purge_expired_alerts()

    try:
        with engine.connect() as conn:
            rows = conn.execute(select(alerts)).fetchall()
    except Exception as e:
        logger.error(f"DB unavailable during alert check, skipping: {e}")
        return

    logger.info(f"Found {len(rows)} active alerts")

    for row in rows:
        logger.info(f"Checking alert ID {row.id}")

        # Skip if already notified or buzzer already running
        if row.notified:
            logger.info("Already notified, skipping")
            continue
        if row.id in active_buzzers:
            logger.info("Buzzer already running, skipping")
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
            if status not in NO_AVAILABILITY_STATUSES:
                logger.info(f"Bookable status found for alert {row.id} — starting buzzer")

                # Mark this alert as actively buzzing
                active_buzzers[row.id] = True

                # Send the first buzz immediately
                send_buzz_message(
                    row.telegram_chat_id,
                    row.id,
                    f"🔔 *SEATS AVAILABLE — BOOK NOW!*\n\n"
                    f"Train: *{row.train_number}*\n"
                    f"Route: {row.from_station} → {row.to_station}\n"
                    f"Date: {row.journey_date} | Class: {row.class_code}\n"
                    f"Status: `{status}`\n\n"
                    f"_You'll be notified every 5 seconds until you book or tap_ 🛑 *Stop Alert*.",
                )

                # Schedule a repeating 5-second buzz job
                scheduler.add_job(
                    buzz_alert,
                    "interval",
                    seconds=5,
                    id=f"buzz_{row.id}",
                    replace_existing=True,
                    kwargs={
                        "alert_id": row.id,
                        "chat_id": row.telegram_chat_id,
                        "train_number": row.train_number,
                        "from_station": row.from_station,
                        "to_station": row.to_station,
                        "journey_date": row.journey_date,
                        "class_code": row.class_code,
                    },
                )
                logger.info(f"Buzzer started for alert {row.id}")

        except Exception as e:
            logger.exception(f"Error checking alert {row.id}: {e}")


# ── FastAPI lifecycle ─────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup_event():
    for attempt in range(15):
        try:
            init_db()
            logger.info("Database connected")
            break
        except Exception as e:
            logger.warning(f"Database not ready ({attempt + 1}/15): {e}")
            time.sleep(5)
    else:
        logger.error("Database never became ready — proceeding anyway")

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
    scheduler.add_listener(on_scheduler_error, EVENT_JOB_ERROR)
    scheduler.add_listener(on_scheduler_missed, EVENT_JOB_MISSED)
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
@app.head("/")
def health_head():
    return Response(status_code=200)


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
