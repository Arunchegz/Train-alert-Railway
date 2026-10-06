import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_session = requests.Session()

retry_strategy = Retry(
    total=3,
    backoff_factor=2,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
)

adapter = HTTPAdapter(max_retries=retry_strategy)
_session.mount("https://", adapter)
_session.mount("http://", adapter)

logger = logging.getLogger("train-alert")


def get_status(
    train_no,
    source,
    destination,
    journey_date,      # Expected format: "DD-MM-YYYY" (e.g., "07-10-2026")
    travel_class,      # e.g., "SL", "3A", "2A", "CC", "2S"
    quota="GN",
    user_id="409b738e3c56567ee21b4fdbc9dfd6b0",
    auth_token="f17fdd9dea99d4ca6f62b8ce0ca95e34",
):
    """
    Fetch seat availability status using RailYatri's current endpoint schema.
    """
    url = f"https://www.railyatri.in/api/v2/seat_availability.json"

    params = {
        "train_number": train_no,
        "from_station": source.upper(),
        "to_station": destination.upper(),
        "journey_date": journey_date,
        "class_type": travel_class.upper(),
        "quota": quota.upper(),
        "device_type_id": "4",
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
        "Referer": f"https://www.railyatri.in/m/trains-between-stations-v2/{source.lower()}-to-{destination.lower()}",
        "Origin": "https://www.railyatri.in",
    }

    try:
        response = _session.get(url, params=params, headers=headers, timeout=20)
        response.raise_for_status()

        data = response.json()

        if not data.get("success", False):
            logger.warning("RailYatri returned success=False | train=%s | response=%s", train_no, data)
            return None

        # RailYatri returns seat details under 'seat_availability' or 'data'
        seats = data.get("seat_availability") or data.get("seat_availibility") or []
        if not seats:
            logger.warning("No seat availability data found | train=%s | data=%s", train_no, data)
            return None

        first_record = seats[0]
        status = (
            first_record.get("availablity_status")
            or first_record.get("status")
            or first_record.get("current_status")
        )

        if not status:
            return None

        return str(status).strip().upper()

    except requests.exceptions.RequestException as e:
        logger.error("Network or API error while fetching train %s: %s", train_no, e)
        return None
