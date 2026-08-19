import logging
import os
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Shared session with retry — used by both railway API and Telegram bot calls
_session = requests.Session()

retry_strategy = Retry(
    total=3,
    backoff_factor=2,
    status_forcelist=[429, 500, 502, 503, 504],
)
adapter = HTTPAdapter(max_retries=retry_strategy)
_session.mount("https://", adapter)
_session.mount("http://", adapter)

BOT_API_URL = "https://api.telegram.org/bot"

logger = logging.getLogger("train-alert")


def get_status(
    train_no,
    source,
    destination,
    journey_date,
    travel_class,
    quota="GN"
):
    """
    Fetches seat availability status from Railyatri API.

    Returns:
        str: The availability status (e.g., 'AVAILABLE', 'RAC') if successful.
        None: If a network error, timeout, or server error occurs.
        str: 'ERROR' if the API response indicates a logical error (no data).
    """
    try:
        url = (
            f"https://sa.railyatri.in/api/seat/enquiry/"
            f"{train_no}/{journey_date}/"
            f"{source}/{destination}/"
            f"{travel_class}/{quota}.json"
        )

        response = _session.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            timeout=20,
        )

        response.raise_for_status()
        data = response.json()

        if not data.get("success"):
            logger.warning(f"API returned success=False for train {train_no}")
            return "ERROR"

        seats = data.get("seat_availibility", [])

        if not seats:
            logger.warning(f"No seat data returned for train {train_no}")
            return "ERROR"

        seat = seats[0]
        status = seat.get("availablity_status", "ERROR")

        return str(status).upper()

    except requests.exceptions.Timeout:
        logger.error(f"Timeout fetching status for train {train_no}")
        return None
    except requests.exceptions.ConnectionError:
        logger.error(f"Connection error fetching status for train {train_no}")
        return None
    except requests.exceptions.HTTPError as e:
        logger.error(f"HTTP error fetching status for train {train_no}: {e}")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Request error fetching status for train {train_no}: {e}")
        return None
    except ValueError as e:
        logger.error(f"JSON parsing error for train {train_no}: {e}")
        return "ERROR"
    except Exception as e:
        logger.error(f"Unexpected error fetching status for train {train_no}: {e}")
        return "ERROR"
