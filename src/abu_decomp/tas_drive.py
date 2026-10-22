"""The driving verbs of `decomp tas`: new, press, state, mark, look, stop, all talking to the running page.

One client: every verb is a request to the serve process (`tas.serve.listen`), so the page's owner sees the same
run the agent drives (/video, /audio) and the recording matches what was pressed. Presses are keyed to controller
polls (`--hold-polls`, `--then-wait-polls`), never wall time.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from abu_decomp import DecompError, tas, tas_health, tas_watch
from abu_decomp.config import Profile

TIMEOUT = 300  # a press waits for polls; `look` cuts PNGs


def request(profile: Profile, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    tas.settings(profile)  # the section must be valid
    url = tas_watch.state_url(profile).rsplit("/", 1)[0] + path
    data = None if body is None else json.dumps(body).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=TIMEOUT) as reply:
            return json.load(reply)
    except urllib.error.HTTPError as error:
        try:
            message = json.load(error).get("error", error.reason)
        except ValueError:
            message = error.reason
        raise DecompError("tas.page", str(message)) from error
    except (OSError, ValueError) as error:
        raise DecompError("tas.page", f"cannot reach {url}: {error}") from error


def new(profile: Profile, movie: str | None) -> dict[str, Any]:
    started = request(profile, "/play", {"name": movie}) if movie else request(profile, "/new", {})
    build = tas_health.header(tas_health.read(tas.settings(profile)))
    return {**started, "build": build} if build else started


def press(profile: Profile, buttons: list[str], hold: int, then: int) -> dict[str, Any]:
    return request(profile, "/press", {"buttons": buttons, "hold": hold, "then": then})


def state(profile: Profile) -> dict[str, Any]:
    full = request(profile, "/state")
    full.pop("movies", None)  # `tas list` is the place for those
    full.pop("recordings", None)
    build = tas_health.header(tas_health.read(tas.settings(profile)))
    return {**full, "build": build} if build else full


def mark(profile: Profile, note: str) -> dict[str, Any]:
    return request(profile, "/mark", {"note": note})


def stop(profile: Profile) -> dict[str, Any]:
    return request(profile, "/stop", {})


def look(profile: Profile, frames: int, span: int, seconds: float) -> dict[str, Any]:
    query = urllib.parse.urlencode({"frames": frames, "span": span, "seconds": seconds})
    return request(profile, f"/look?{query}")
