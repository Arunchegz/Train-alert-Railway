import requests


def get_status(
    train_no,
    source,
    destination,
    journey_date,
    travel_class,
    quota="GN"
):
    try:

        url = (
            f"https://sa.railyatri.in/api/seat/enquiry/"
            f"{train_no}/{journey_date}/"
            f"{source}/{destination}/"
            f"{travel_class}/{quota}.json"
        )

        response = requests.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0"
            },
            timeout=20
        )

        response.raise_for_status()

        data = response.json()

        if not data.get("success"):
            return "ERROR"

        seats = data.get("seat_availibility", [])

        if not seats:
            return "ERROR"

        seat = seats[0]

        return str(
            seat.get(
                "availablity_status",
                "ERROR"
            )
        ).upper()

    except Exception as e:
        print("Railyatri Error:", e)
        return "ERROR"
