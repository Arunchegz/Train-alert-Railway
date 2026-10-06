import logging
import os
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Bot API URL required by telegram_bot.py
BOT_API_URL = os.environ.get("BOT_API_URL", "https://api.telegram.org/bot")

# RailYatri default credentials (can be overridden via environment variables)
DEFAULT_USER_ID = os.environ.get("RAILYATRI_USER_ID", "409b738e3c56567ee21b4fdbc9dfd6b0")
DEFAULT_AUTH_TOKEN = os.environ.get("RAILYATRI_AUTH_TOKEN", "f17fdd9dea99d4ca6f62b8ce0ca95e34")

_session = requests.Session()

retry_strategy = Retry(
    total=3,
    backoff_factor=2,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "POST"],
)

adapter = HTTPAdapter(max_retries=retry_strategy)
_session.mount("https://", adapter)
_session.mount("http://", adapter)

logger = logging.getLogger("train-alert")


def normalize_date(journey_date: str) -> str:
    """
    Normalize journey date string into YYYY-MM-DD format.
    Accepts formats: DD-MM-YYYY, D-M-YYYY, or YYYY-MM-DD.
    """
    journey_date = str(journey_date).strip()
    if "-" in journey_date:
        parts = journey_date.split("-")
        if len(parts) == 3:
            if len(parts[0]) == 4:
                # Already YYYY-MM-DD
                return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
            else:
                # DD-MM-YYYY or D-M-YYYY -> YYYY-MM-DD
                return f"{int(parts[2]):04d}-{int(parts[1]):02d}-{int(parts[0]):02d}"
    return journey_date


def get_status(
    train_no,
    source,
    destination,
    journey_date,      # Format: "DD-MM-YYYY", "D-M-YYYY", or "YYYY-MM-DD"
    travel_class,      # e.g., "SL", "3A", "2A", "CC", "2S"
    quota="GN",
    user_id=None,
    auth_token=None,
):
    """
    Fetch seat availability status using RailYatri's latest endpoint schema:
    https://sa.railyatri.in/api/seat/enquiry/{train_no}/{journey_date}/{from}/{to}/{class}/{quota}.json

    Returns:
        str: Availability status such as AVAILABLE-0048, CURR_AVBL-0088, RAC, GNWL..., NOT AVAILABLE.
        None: Temporary network error or upstream IRCTC error (so scheduler retries later).
    """
    user_id = user_id or DEFAULT_USER_ID
    auth_token = auth_token or DEFAULT_AUTH_TOKEN
    clean_date = normalize_date(journey_date)

    url = (
        f"https://sa.railyatri.in/api/seat/enquiry/"
        f"{str(train_no).strip()}/"
        f"{clean_date}/"
        f"{source.strip().upper()}/"
        f"{destination.strip().upper()}/"
        f"{travel_class.strip().upper()}/"
        f"{quota.strip().upper()}.json"
    )

    params = {
        "device_type_id": "4",
        "src": "ttb_landing",
        "utm_source": "mwebhome_search_trains",
        "is_update": "true",
        "update_duration": "0",
        "d_day": "0",
        "user_id": user_id,
        "authentication_token": auth_token,
    }

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 10; Mobile) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Mobile Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": f"https://www.railyatri.in/m/trains-between-stations-v2/{source.strip().lower()}-to-{destination.strip().lower()}",
        "Origin": "https://www.railyatri.in",
    }

    try:
        response = _session.get(url, params=params, headers=headers, timeout=20)
        response.raise_for_status()

        try:
            data = response.json()
        except ValueError:
            logger.error(
                "RailYatri returned non-JSON response | train=%s | response=%s",
                train_no,
                response.text[:500],
            )
            return None

        if not data.get("success", False):
            msg = data.get("message") or data.get("error")
            logger.warning("RailYatri returned success=False | train=%s | message=%s", train_no, msg)
            return None

        # Check for upstream IRCTC error or messages
        error_msg = data.get("error")
        if error_msg:
            error_upper = str(error_msg).upper()
            if "VALID TRAIN NUMBER" in error_upper:
                return "NOT FOUND"
            if "DEPARTED" in error_upper:
                return "TRAIN DEPARTED"
            logger.warning("RailYatri reported error for train %s: %s", train_no, error_msg)
            # If IRCTC is undergoing maintenance or temporarily unable to process
            if "UNABLE TO PROCESS" in error_upper or "TRY AGAIN" in error_upper:
                return None

        seats = data.get("seat_availibility") or data.get("seat_availability") or []
        if not seats:
            logger.warning("No seat availability data found | train=%s | data=%s", train_no, data)
            return None

        # Look for the seat record matching the target date
        target_record = seats[0]
        # Attempt to match by date if multiple dates are present in 6-day forecast
        d_parts = clean_date.split("-")
        day_variants = {
            f"{int(d_parts[2])}-{int(d_parts[1])}-{d_parts[0]}",
            f"{int(d_parts[2]):02d}-{int(d_parts[1]):02d}-{d_parts[0]}",
        }
        for rec in seats:
            rec_date = rec.get("availablity_date") or rec.get("Date (DD-MM-YYYY)")
            if rec_date in day_variants:
                target_record = rec
                break

        status = (
            target_record.get("availablity_status")
            or target_record.get(f"Class - {travel_class.strip().upper()}")
            or target_record.get(travel_class.strip().upper())
            or target_record.get("status")
            or target_record.get("current_status")
        )

        if not status:
            return None

        return str(status).strip().upper()

    except requests.exceptions.RequestException as e:
        logger.error("Network or API error while fetching train %s: %s", train_no, e)
        return None


def get_trains_between_stations(
    source,
    destination,
    journey_date,      # Expected format: "DD-MM-YYYY" or "YYYY-MM-DD"
    user_id=None,
    auth_token=None,
):
    """
    Fetch trains running between two stations on a given date using RailYatri's
    trains-between-stations API:
    https://trainticketapi.railyatri.in/api/trains-between-station-with-sa.json
    """
    user_id = user_id or DEFAULT_USER_ID
    auth_token = auth_token or DEFAULT_AUTH_TOKEN

    # Convert to DD-MM-YYYY format expected by trainticketapi
    journey_date = str(journey_date).strip()
    if "-" in journey_date:
        parts = journey_date.split("-")
        if len(parts) == 3 and len(parts[0]) == 4:
            # YYYY-MM-DD -> DD-MM-YYYY
            formatted_date = f"{int(parts[2]):02d}-{int(parts[1]):02d}-{parts[0]}"
        elif len(parts) == 3:
            formatted_date = f"{int(parts[0]):02d}-{int(parts[1]):02d}-{parts[2]}"
        else:
            formatted_date = journey_date
    else:
        formatted_date = journey_date

    url = "https://trainticketapi.railyatri.in/api/trains-between-station-with-sa.json"
    params = {
        "from": source.strip().upper(),
        "to": destination.strip().upper(),
        "dateOfJourney": formatted_date,
        "device_type_id": "4",
        "user_id": user_id,
        "authentication_token": auth_token,
        "utm_source": "mwebhome_search_trains",
    }
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 10; Mobile) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Mobile Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.railyatri.in",
        "Referer": f"https://www.railyatri.in/m/trains-between-stations-v2/{source.strip().lower()}-to-{destination.strip().lower()}",
    }

    try:
        response = _session.get(url, params=params, headers=headers, timeout=20)
        response.raise_for_status()
        data = response.json()
        if data.get("success", False):
            return data.get("train_between_stations", [])
        return []
    except Exception as e:
        logger.error("Error fetching trains between stations: %s", e)
        return []
