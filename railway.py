import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Shared session with retry
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

BOT_API_URL = "https://api.telegram.org/bot"

logger = logging.getLogger("train-alert")


def get_status(
    train_no,
    source,
    destination,
    journey_date,
    travel_class,
    quota="GN",
):
    """
    Fetch seat availability status from RailYatri API.

    Returns:
        str:
            Availability status such as AVAILABLE, RAC, WL, REGRET, etc.

        None:
            API/network/logical failure. The scheduler should retry later.

    This function does NOT treat an API failure as a genuine
    non-bookable train status.
    """

    url = (
        f"https://sa.railyatri.in/api/seat/enquiry/"
        f"{train_no}/{journey_date}/"
        f"{source}/{destination}/"
        f"{travel_class}/{quota}.json"
    )

    logger.info(
        "Checking RailYatri API | "
        "train=%s | from=%s | to=%s | date=%s | class=%s | quota=%s",
        train_no,
        source,
        destination,
        journey_date,
        travel_class,
        quota,
    )

    try:
        response = _session.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json",
            },
            timeout=20,
        )

        logger.info(
            "RailYatri HTTP response | train=%s | status_code=%s",
            train_no,
            response.status_code,
        )

        response.raise_for_status()

        # Try to decode JSON
        try:
            data = response.json()
        except ValueError:
            logger.error(
                "RailYatri returned invalid JSON | "
                "train=%s | response=%s",
                train_no,
                response.text[:2000],
            )
            return None

        # Log the complete API response when success=False.
        # This is important for diagnosing the current problem.
        if not data.get("success"):
            logger.warning(
                "RailYatri API returned success=False | "
                "train=%s | response=%s",
                train_no,
                data,
            )
            return None

        # Get seat availability list
        seats = data.get("seat_availibility", [])

        if not seats:
            logger.warning(
                "RailYatri returned no seat data | "
                "train=%s | response=%s",
                train_no,
                data,
            )
            return None

        # Use first availability record
        seat = seats[0]

        status = seat.get("availablity_status")

        if not status:
            logger.warning(
                "RailYatri seat record has no availability status | "
                "train=%s | seat=%s",
                train_no,
                seat,
            )
            return None

        status = str(status).strip().upper()

        logger.info(
            "RailYatri availability | "
            "train=%s | class=%s | status=%s",
            train_no,
            travel_class,
            status,
        )

        return status

    except requests.exceptions.Timeout:
        logger.error(
            "RailYatri API timeout | train=%s",
            train_no,
        )
        return None

    except requests.exceptions.ConnectionError as e:
        logger.error(
            "RailYatri API connection error | "
            "train=%s | error=%s",
            train_no,
            e,
        )
        return None

    except requests.exceptions.HTTPError as e:
        logger.error(
            "RailYatri API HTTP error | "
            "train=%s | status_code=%s | error=%s | response=%s",
            train_no,
            response.status_code if "response" in locals() else "N/A",
            e,
            response.text[:2000] if "response" in locals() else "N/A",
        )
        return None

    except requests.exceptions.RequestException as e:
        logger.error(
            "RailYatri API request error | "
            "train=%s | error=%s",
            train_no,
            e,
        )
        return None

    except Exception as e:
        logger.exception(
            "Unexpected error while checking train %s: %s",
            train_no,
            e,
        )
        return None
