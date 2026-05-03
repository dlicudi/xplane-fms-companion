"""Simbrief OFP route fetcher.

Fetches the latest OFP for a given Simbrief pilot ID or username and returns
the full ICAO route string ready for the route parser.
"""

from __future__ import annotations

import ssl
import threading
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Callable, Optional

_API_URL = "https://www.simbrief.com/api/xml.fetcher.php"


def fetch_route(pilot_id: str, callback: Callable[[Optional[str], Optional[str]], None]) -> None:
    """Fetch the latest Simbrief OFP route in a background thread.

    callback(route_str, error) is called on completion — one of them will be None.
    pilot_id may be a numeric user ID or a username string.
    """
    threading.Thread(target=_fetch, args=(pilot_id.strip(), callback), daemon=True).start()


def _fetch(pilot_id: str, callback: Callable) -> None:
    try:
        param = "userid" if pilot_id.isdigit() else "username"
        url = f"{_API_URL}?{param}={urllib.parse.quote(pilot_id)}"
        req = urllib.request.Request(url, headers={"User-Agent": "XPlane-FMSCompanion/1.0"})
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            raw = resp.read()

        root = ET.fromstring(raw)

        status = root.findtext("fetch/status") or ""
        if status.lower() == "error":
            callback(None, root.findtext("fetch/message") or "Simbrief returned an error")
            return

        origin = (root.findtext("origin/icao_code") or "").strip()
        dest   = (root.findtext("destination/icao_code") or "").strip()
        route  = (root.findtext("general/route") or "").strip()

        if not origin or not dest:
            callback(None, "Missing origin/destination in Simbrief response")
            return

        # Strip leading/trailing airport idents if Simbrief included them in the route field.
        tokens = route.split()
        if tokens and tokens[0] == origin:
            tokens = tokens[1:]
        if tokens and tokens[-1] == dest:
            tokens = tokens[:-1]
        route = " ".join(tokens)

        full = f"{origin} {route} {dest}" if route else f"{origin} {dest}"
        callback(full, None)

    except Exception as exc:
        callback(None, str(exc))


