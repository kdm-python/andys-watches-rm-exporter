"""Read-only Click & Drop adapter. No labels, statuses, or manifests are changed."""

from datetime import datetime, timezone
from math import ceil
from threading import Event
from time import monotonic
from urllib.parse import quote

import requests

from rm_exporter.dispatch_schema import DispatchRequest


class PollHTTPError(Exception):
    def __init__(self, message, *, retryable=True, retry_after=0, status_code=None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after
        self.status_code = status_code


def retry_after(response):
    try:
        return min(3600, max(1, int(response.headers.get("Retry-After", "0"))))
    except (TypeError, ValueError):
        return 0


class RoyalMailReader:
    def __init__(self, base_url, api_key, stop=None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.stop = stop or Event()
        self.last_request = None
        self.cooldown_until = 0

    def _get(self, path):
        remaining = ceil(self.cooldown_until - monotonic())
        if remaining > 0:
            raise PollHTTPError(
                "Royal Mail rate-limit cooldown is active",
                status_code=429,
                retry_after=remaining,
            )
        # Leave rate-limit headroom for the independent outbound exporter.
        if self.last_request is not None:
            if self.stop.wait(max(0, 1 - (monotonic() - self.last_request))):
                raise InterruptedError()
        self.last_request = monotonic()
        try:
            response = requests.get(
                f"{self.base_url}/{path}",
                headers={"Authorization": self.api_key},
                timeout=(5, 30),
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise PollHTTPError("Royal Mail connection failed or timed out") from exc
        if response.status_code != 200:
            delay = retry_after(response)
            if response.status_code == 429:
                delay = max(30, delay)
                self.cooldown_until = monotonic() + delay
            raise PollHTTPError(
                f"Royal Mail returned HTTP {response.status_code}",
                retry_after=delay,
                status_code=response.status_code,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise PollHTTPError("Royal Mail returned invalid JSON") from exc

    def connectivity(self):
        """Make an authenticated, read-only account request to Click & Drop."""
        carriers = self._get("carriers")
        if not isinstance(carriers, list) or any(
            not isinstance(carrier, dict) for carrier in carriers
        ):
            raise PollHTTPError("Expected the Click & Drop carrier array")
        return carriers

    def orders(self, references):
        if not 1 <= len(references) <= 100:
            raise ValueError("Request between 1 and 100 Royal Mail references")
        identifiers = ";".join('"' + quote(ref, safe="") + '"' for ref in references)
        rows = self._get(f"orders/{identifiers}")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise PollHTTPError("Expected the ordinary Click & Drop order lookup array")
        return rows


def timestamp(value, field):
    if (
        value is None
        or value == ""
        or (isinstance(value, str) and value.startswith("0001-01-01"))
    ):
        return None
    try:
        if not isinstance(value, str) or "T" not in value:
            raise ValueError("not a timestamp")
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # Live personal-account responses omit the timezone. We only compare
        # state transitions, not instants: retain that local/unspecified value
        # without inventing a UTC offset. Normalize explicit offsets to UTC.
        return (
            date.astimezone(timezone.utc).isoformat()
            if date.tzinfo
            else date.isoformat()
        )
    except (ValueError, TypeError, AttributeError) as exc:
        raise PollHTTPError(f"Invalid {field}; expected an ISO date and time") from exc


def observation(raw):
    if not raw.get("orderIdentifier") or not isinstance(raw.get("orderReference"), str):
        raise PollHTTPError(
            "Royal Mail lookup is missing its order identifier/reference"
        )
    packages = raw.get("packages") or []
    if not isinstance(packages, list) or any(not isinstance(p, dict) for p in packages):
        raise PollHTTPError("Royal Mail returned invalid package tracking")

    def tracking(value):
        if value is None:
            return None
        if not isinstance(value, str):
            raise PollHTTPError("Royal Mail returned a non-string tracking number")
        return value.strip() or None

    # Persist/log only fields needed to determine dispatch, never customer data.
    return {
        "orderIdentifier": raw["orderIdentifier"],
        "orderReference": raw["orderReference"],
        "printedOn": timestamp(raw.get("printedOn"), "printedOn"),
        "shippedOn": timestamp(raw.get("shippedOn"), "shippedOn"),
        "manifestedOn": timestamp(raw.get("manifestedOn"), "manifestedOn"),
        "trackingNumber": tracking(raw.get("trackingNumber")),
        "packages": [
            {
                "packageNumber": p.get("packageNumber"),
                "trackingNumber": tracking(p.get("trackingNumber")),
            }
            for p in packages
        ],
    }


def classify(row, current):
    if row["first_observed_at"] is None:
        return "historical" if current["shippedOn"] else "watching"
    previous = row.get("observation") or {}
    if previous.get("orderIdentifier") != current["orderIdentifier"]:
        return "review"
    if previous.get("shippedOn") and previous["shippedOn"] != current["shippedOn"]:
        return "review"
    if current["shippedOn"] and not row["baseline_shipped_on"]:
        return "detected"
    return "watching"


def dispatch_payload(order_id, current):
    numbers = [
        p.get("trackingNumber") for p in current["packages"] if p.get("trackingNumber")
    ]
    numbers = numbers or (
        [current["trackingNumber"]] if current["trackingNumber"] else []
    )
    labels = [
        {"tracking_number": number, "tracking_url": "", "label_url": ""}
        for number in dict.fromkeys(numbers)
    ]
    # Do not invent tracking or postage-label URLs. Numbers alone are supported.
    return DispatchRequest.model_validate(
        {
            "order_id": order_id,
            "status": "despatched",
            "labels": labels or None,
        }
    ).model_dump(exclude_none=True)
