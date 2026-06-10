import logging
import os
import requests
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)
from sqlalchemy import insert, select, delete
from models import engine, alerts

# ── Config ───────────────────────────────────────────────────────────────────
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("train-alert-bot")

# ── Conversation states ───────────────────────────────────────────────────────
(
    TRAIN_NUMBER,
    FROM_STATION,
    TO_STATION,
    JOURNEY_DATE,
    CLASS_CODE,
    CONFIRM,
) = range(6)

CLASS_OPTIONS = [["SL", "3A", "2A"], ["1A", "CC", "2S"], ["EC", "FC"]]


# ── Helpers ───────────────────────────────────────────────────────────────────
def send_alert(chat_id: str, message: str) -> None:
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=30,
        )
    except Exception as exc:
        logger.error("Failed to send alert to %s: %s", chat_id, exc)


def _save_alert(data: dict, chat_id: str) -> None:
    with engine.connect() as conn:
        conn.execute(
            insert(alerts).values(
                train_number=data["train_number"],
                from_station=data["from_station"],
                to_station=data["to_station"],
                journey_date=data["journey_date"],
                class_code=data["class_code"],
                telegram_chat_id=str(chat_id),
                notified=False,
            )
        )
        conn.commit()


# ── /start & /help ────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Welcome to *Train Alert Bot*!\n\n"
        "I'll notify you the moment a ticket becomes available on IRCTC.\n\n"
        "Commands:\n"
        "  /addalert     — Add a new train alert\n"
        "  /myalerts     — View your active alerts\n"
        "  /deletealert  — Delete a saved alert\n"
        "  /cancel       — Cancel current operation\n"
        "  /help         — Show this message",
        parse_mode="Markdown",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


# ── /myalerts ─────────────────────────────────────────────────────────────────
async def my_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    with engine.connect() as conn:
        rows = conn.execute(
            select(alerts).where(alerts.c.telegram_chat_id == chat_id)
        ).fetchall()

    if not rows:
        await update.message.reply_text("You have no active alerts.")
        return

    lines = ["📋 *Your alerts:*\n"]
    for i, row in enumerate(rows, 1):
        status = "✅ Notified" if row.notified else "⏳ Watching"
        lines.append(
            f"{i}. Train *{row.train_number}* | {row.from_station} → {row.to_station}\n"
            f"   Date: {row.journey_date} | Class: {row.class_code} | {status}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /addalert conversation ────────────────────────────────────────────────────
async def add_alert_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "🚆 Let's set up a new alert.\n\n"
        "Enter the *train number* (e.g. 12345):",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return TRAIN_NUMBER


async def recv_train_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("⚠️ Please enter a valid numeric train number:")
        return TRAIN_NUMBER
    context.user_data["train_number"] = text
    await update.message.reply_text(
        "Enter the *FROM station code* (e.g. NDLS for New Delhi):",
        parse_mode="Markdown",
    )
    return FROM_STATION


async def recv_from_station(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["from_station"] = update.message.text.strip().upper()
    await update.message.reply_text(
        "Enter the *TO station code* (e.g. MMCT for Mumbai Central):",
        parse_mode="Markdown",
    )
    return TO_STATION


async def recv_to_station(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["to_station"] = update.message.text.strip().upper()
    await update.message.reply_text(
        "Enter the *journey date* in DD-MM-YYYY format (e.g. 25-07-2025):",
        parse_mode="Markdown",
    )
    return JOURNEY_DATE


async def recv_journey_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    parts = text.split("-")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        await update.message.reply_text(
            "⚠️ Please use DD-MM-YYYY format (e.g. 25-07-2025):"
        )
        return JOURNEY_DATE
    context.user_data["journey_date"] = text
    await update.message.reply_text(
        "Choose the *travel class*:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(
            CLASS_OPTIONS, one_time_keyboard=True, resize_keyboard=True
        ),
    )
    return CLASS_CODE


async def recv_class_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = update.message.text.strip().upper()
    valid = {c for row in CLASS_OPTIONS for c in row}
    if code not in valid:
        await update.message.reply_text(
            f"⚠️ Please pick a class from the keyboard. Valid options: {', '.join(sorted(valid))}"
        )
        return CLASS_CODE
    context.user_data["class_code"] = code

    d = context.user_data
    summary = (
        f"📝 *Alert summary:*\n\n"
        f"Train  : {d['train_number']}\n"
        f"From   : {d['from_station']}\n"
        f"To     : {d['to_station']}\n"
        f"Date   : {d['journey_date']}\n"
        f"Class  : {d['class_code']}\n\n"
        f"Confirm? (yes / no)"
    )
    await update.message.reply_text(
        summary,
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(
            [["Yes", "No"]], one_time_keyboard=True, resize_keyboard=True
        ),
    )
    return CONFIRM


async def recv_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    answer = update.message.text.strip().lower()
    if answer in ("yes", "y"):
        try:
            _save_alert(context.user_data, update.effective_chat.id)
            await update.message.reply_text(
                "✅ Alert saved! I'll notify you as soon as a seat opens.",
                reply_markup=ReplyKeyboardRemove(),
            )
        except Exception as exc:
            logger.exception("Error saving alert: %s", exc)
            await update.message.reply_text(
                "❌ Something went wrong while saving. Please try /addalert again.",
                reply_markup=ReplyKeyboardRemove(),
            )
    else:
        await update.message.reply_text(
            "❌ Alert cancelled. Use /addalert to start over.",
            reply_markup=ReplyKeyboardRemove(),
        )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text(
        "Operation cancelled.", reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


# ── /deletealert ──────────────────────────────────────────────────────────────
async def delete_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)

    with engine.connect() as conn:
        rows = conn.execute(
            select(alerts).where(alerts.c.telegram_chat_id == chat_id)
        ).fetchall()

    if not rows:
        await update.message.reply_text("You have no alerts to delete.")
        return

    if not context.args:
        lines = ["Which alert do you want to delete?\nUse: /deletealert <number>\n"]
        for i, row in enumerate(rows, 1):
            status = "✅ Notified" if row.notified else "⏳ Watching"
            lines.append(
                f"{i}. Train *{row.train_number}* | {row.from_station} → {row.to_station}\n"
                f"   Date: {row.journey_date} | Class: {row.class_code} | {status}"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    try:
        index = int(context.args[0])
        if not (1 <= index <= len(rows)):
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            f"⚠️ Please provide a number between 1 and {len(rows)}.\n"
            f"Use /deletealert to see the list."
        )
        return

    target = rows[index - 1]
    with engine.connect() as conn:
        conn.execute(delete(alerts).where(alerts.c.id == target.id))
        conn.commit()

    await update.message.reply_text(
        f"🗑️ Deleted alert for train *{target.train_number}* "
        f"({target.from_station} → {target.to_station}, "
        f"{target.journey_date}, {target.class_code}).",
        parse_mode="Markdown",
    )


# ── App builder ───────────────────────────────────────────────────────────────
def build_application() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("addalert", add_alert_start)],
        states={
            TRAIN_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_train_number)],
            FROM_STATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_from_station)],
            TO_STATION:   [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_to_station)],
            JOURNEY_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_journey_date)],
            CLASS_CODE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_class_code)],
            CONFIRM:      [MessageHandler(filters.TEXT & ~filters.COMMAND, recv_confirm)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("myalerts", my_alerts))
    app.add_handler(CommandHandler("deletealert", delete_alert))
    app.add_handler(conv_handler)

    return app
