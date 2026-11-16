"""What a build says about itself while it is still moving.

The factual part is the headline — what a visitor needs to know. Under it, a
flavour line rotates as the build waits, so a page that polls reads as progress
rather than a frozen sentence.

Rotation is computed, not scripted: the line index is derived from how long the
build has sat in its current status (``updated_at`` is bumped at every status
change), so every viewer of the same build sees the same line, and the htmx
poll that already exists is the only thing making it move. Nothing inline runs
in the browser, which keeps both pages inside their CSP.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

from app.domain.models import Build

#: Seconds a flavour line stays up. Columns poll every 1.5s, so a line changes
#: every six polls: it reads as progress, not as a slot machine.
ROTATE_SECONDS = 9

#: One factual sentence per status. ``{endpoint}`` is filled in where it fits.
HEADLINES = {
    "queued": "queued — waiting for a worker slot",
    "generating": "generating — asking {endpoint}…",
    "validating": "validating the artifact…",
    "published": "published — nothing published to show",
    "rejected": "rejected — the model answered, but the artifact did not pass the gate",
    #: Used instead of the plain rejection headline when the failure history
    #: says the answer was cut off at the token ceiling — a different problem,
    #: and one the visitor can fix by asking for something smaller.
    "rejected-truncated": "rejected — the answer ran out of tokens before the document finished",
    "failed": "failed — the endpoint or the job did not finish",
    "cancelled": "cancelled",
}

#: Deadpan company for the three statuses where work is actually happening.
#: Terminal statuses get the headline alone: nothing is in progress any more.
FLAVOUR = {
    "queued": (
        "still just a sentence",
        "first come, first served",
        "warming up a worker",
        "every app begins in a queue",
    ),
    "generating": (
        "the model is typing",
        "one whole page, typed from scratch",
        "drawing pixels it has never seen",
        "there is no undo at this stage",
    ),
    "validating": (
        "gate time: one file, no outside calls",
        "measuring it against the size ceiling",
        "checking that it cannot phone home",
        "single file, or straight back to the pile",
    ),
}


def build_report(
    build: Build,
    endpoint_name: str | None = None,
    *,
    failures: Sequence[dict] | None = None,
    now: dt.datetime | None = None,
) -> dict:
    """The copy for one build's standing-in: headline, rotating flavour, elapsed.

    ``failures`` is the build's recorded failure history (the same rows the
    admin panel reads): if a rejection was caused by an answer hitting the
    token ceiling, the headline says so instead of blaming the artifact alone.

    ``now`` is a parameter only so a test can spin the clock; callers leave it
    out and get the wall clock. An unparseable timestamp degrades to "just
    started" rather than to no message at all.
    """
    status = str(build.status)
    if status == "rejected" and failures and any(str(f.get("stage")) == "truncated" for f in failures):
        status = "rejected-truncated"
    headline = HEADLINES.get(status, status).format(endpoint=endpoint_name or "the endpoint")
    lines = FLAVOUR.get(status)
    if lines is None:
        return {"headline": headline, "flavour": None, "elapsed": None}

    started = _parse_ts(build.updated_at)
    reference = now or dt.datetime.now(dt.timezone.utc)
    elapsed = 0 if started is None else max(0, int((reference - started).total_seconds()))
    return {
        "headline": headline,
        "flavour": lines[(elapsed // ROTATE_SECONDS) % len(lines)],
        "elapsed": elapsed,
    }


def _parse_ts(raw: str) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.timezone.utc)
