import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, date

from fastapi import FastAPI, Form, Request, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import insert, select, update, delete, and_
from telegram import Update
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED

from models import engine, alerts, init_db
from railway import get_status
from telegram_bot import send_alert, send_buzz_message, stop_buzzer, build_application, BOT_TOKEN, WEBHOOK_URL
from scheduler import scheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("train-alert")

ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")
# Fix #4: secret key for /test-check endpoint
TEST_CHECK_SECRET = os.environ.get("TEST_CHECK_SECRET", "")

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
        with engine.connect() as conn:
            conn.execute(
                update(alerts)
                .where(alerts.c.id.in_(expired))
                .values(is_buzzing=False)
            )
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
    try:
        with engine.connect() as conn:
            row = conn.execute(
                select(alerts).where(alerts.c.id == alert_id)
            ).first()

            if not row or not row.is_buzzing:
                try:
                    scheduler.remove_job(f"buzz_{alert_id}")
                except Exception:
                    pass
                return
    except Exception as e:
        logger.error(f"Error checking DB state for alert {alert_id}: {e}")
        return

    try:
        status = get_status(train_number, from_station, to_station, journey_date, class_code)
    except Exception as e:
        logger.error(f"Buzz re-check error for alert {alert_id}: {e}")
        return

    if status is None:
        logger.warning(f"Network error for alert {alert_id}, skipping notification.")
        return

    status = str(status).strip().upper()

    if status in NO_AVAILABILITY_STATUSES:
        logger.info(f"Seats gone for alert {alert_id}, stopping buzzer (status={status})")

        try:
            with engine.connect() as conn:
                conn.execute(
                    update(alerts)
                    .where(alerts.c.id == alert_id)
                    .values(is_buzzing=False)
                )
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to update DB for alert {alert_id}: {e}")

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

    try:
        with engine.connect() as conn:
            rows = conn.execute(select(alerts)).fetchall()
    except Exception as e:
        logger.error(f"DB unavailable during alert check, skipping: {e}")
        return

    logger.info(f"Found {len(rows)} active alerts")

    for row in rows:
        logger.info(f"Checking alert ID {row.id}")

        if row.notified:
            logger.info("Already notified, skipping")
            continue

        if row.is_buzzing:
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

            if status is None:
                logger.warning(f"Network error checking alert {row.id}, skipping.")
                _update_last_check(row.id, "NETWORK_ERROR", datetime.now())
                continue

            status = str(status).strip().upper()
            _update_last_check(row.id, status, datetime.now())

            if status in NO_AVAILABILITY_STATUSES:
                logger.info(f"Non-bookable status for alert {row.id}: {status}")
                continue

            alert_on = row.alert_on if row.alert_on else "AVAILABLE"
            should_trigger = False

            if alert_on == "AVAILABLE":
                if status.startswith("AVAILABLE") or status.startswith("AVBL") or status.startswith("CURR_AVBL"):
                    should_trigger = True
            elif alert_on == "RAC":
                if (status.startswith("AVAILABLE") or status.startswith("AVBL") or
                        status.startswith("CURR_AVBL") or status.startswith("RAC")):
                    should_trigger = True
            elif alert_on == "WL":
                if status not in NO_AVAILABILITY_STATUSES:
                    should_trigger = True

            if not should_trigger:
                logger.info(f"Condition not met for alert {row.id} (alert_on={alert_on}, status={status})")
                continue

            logger.info(f"Bookable status found for alert {row.id} — starting buzzer")

            # Fix #6: atomic check-and-set to prevent double-spawn race condition
            # Only update (and proceed) if is_buzzing is still False in DB
            try:
                with engine.connect() as conn:
                    result = conn.execute(
                        update(alerts)
                        .where(and_(alerts.c.id == row.id, alerts.c.is_buzzing == False))
                        .values(is_buzzing=True)
                    )
                    conn.commit()

                if result.rowcount == 0:
                    # Another cycle already claimed this alert
                    logger.info(f"Alert {row.id} already claimed by another cycle, skipping")
                    continue
            except Exception as e:
                logger.error(f"Failed to atomically set is_buzzing for alert {row.id}: {e}")
                continue

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


def _update_last_check(alert_id: int, status: str, dt: datetime) -> None:
    """Helper to update last_checked_status and last_checked_time in DB."""
    try:
        # Fix #8: store as ISO timestamp for consistency
        time_str = dt.isoformat(timespec="seconds")
        with engine.connect() as conn:
            conn.execute(
                update(alerts)
                .where(alerts.c.id == alert_id)
                .values(
                    last_checked_status=status,
                    last_checked_time=time_str,
                )
            )
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to update last check info for alert {alert_id}: {e}")


# ── FastAPI lifespan (fix #9: replaces deprecated @app.on_event) ──────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
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

    _bot_app = build_application()
    app.state.bot_app = _bot_app

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
        minutes=4,
        id="train_alert_checker",
        replace_existing=True,
    )
    # Fix #11: purge only via midnight cron, not inside check_alerts
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

    yield

    # Shutdown
    await _bot_app.stop()
    await _bot_app.shutdown()
    scheduler.shutdown()
    logger.info("Shutdown complete")


app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="templates")


# ── Webhook endpoint ──────────────────────────────────────────────────────────
@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    tg_update = Update.de_json(data, request.app.state.bot_app.bot)
    await request.app.state.bot_app.process_update(tg_update)
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
    alert_on: str = Form("AVAILABLE"),  # Fix #5: expose alert_on in web form
):
    valid_alert_on = {"AVAILABLE", "RAC", "WL"}
    if alert_on not in valid_alert_on:
        alert_on = "AVAILABLE"

    with engine.connect() as conn:
        conn.execute(
            insert(alerts).values(
                train_number=train_number,
                from_station=from_station.strip().upper(),
                to_station=to_station.strip().upper(),
                journey_date=journey_date,
                class_code=class_code.strip().upper(),
                telegram_chat_id=telegram_chat_id,
                notified=False,
                is_buzzing=False,
                alert_on=alert_on,
                last_checked_status=None,
                last_checked_time=None,
            )
        )
        conn.commit()
    return {"success": True, "message": "Alert saved"}


@app.get("/test-check")
def test_check(x_secret: str = Header(default="")):
    # Fix #4: guard with secret key
    if TEST_CHECK_SECRET and x_secret != TEST_CHECK_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    check_alerts()
    return {"success": True, "message": "Manual check completed"}
