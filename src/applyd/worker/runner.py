"""Apply worker: polls public.applications for claimable work, dispatches.

Polling loop:
  every N seconds, find one row WHERE status IN ('tailored', 'failed'),
  hand it to `applyd.apply.saas.apply_for_user`, which handles the atomic
  claim, the Playwright/Bright Data tool-use loop, and the cost accounting.

Entry: python -m applyd.worker.runner [--poll-seconds 30]

The polling-then-claiming pattern is safe under concurrency because the claim
itself is a single UPDATE...WHERE status IN (...) — Postgres serializes it, so
two workers selecting the same row results in exactly one successful claim.

Exits cleanly on SIGINT.
"""

from __future__ import annotations

import argparse
import logging
import signal
import time
from typing import Optional

from applyd.config import load_env

load_env()

from applyd.apply.saas import apply_for_user  # noqa: E402
from applyd.db import get_client  # noqa: E402

logger = logging.getLogger("applyd.worker")

# Only freshly-tailored rows are claimable. 'failed' is terminal — a previous
# attempt already burned cost and wrote an apply_attempts audit row. To retry
# manually, flip the row back to 'tailored' in the DB.
CLAIMABLE = ("tailored",)


def _find_one_claimable() -> Optional[dict]:
    sb = get_client()
    res = (
        sb.table("applications")
        .select("id, user_id, job_id, status")
        .in_("status", list(CLAIMABLE))
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def tick_once() -> Optional[dict]:
    """One iteration of the polling loop.

    Returns the result dict from `apply_for_user` on success, or None if
    nothing was claimable.
    """
    candidate = _find_one_claimable()
    if candidate is None:
        return None

    app_id = candidate["id"]
    user_id = candidate["user_id"]

    logger.info("[worker] dispatching app %s for user %s", app_id, user_id)
    print(f"[worker] dispatching app {app_id} for user {user_id}")

    try:
        result = apply_for_user(user_id=user_id, application_id=app_id)
    except Exception as exc:  # noqa: BLE001
        # apply_for_user already releases the app on internal failures; this
        # only fires if something raises *before* the claim (e.g. ValueError
        # for a missing row, or a network blip while looking it up).
        logger.exception("[worker] apply_for_user crashed for %s: %s", app_id, exc)
        return None

    logger.info(
        "[worker] app %s done: status=%s cost_cents=%s",
        app_id,
        result.get("status"),
        result.get("cost_cents"),
    )
    return result


def run_forever(poll_seconds: float = 30.0) -> None:
    stop = {"flag": False}

    def _handle(signum, _frame):  # noqa: ANN001
        logger.info("[worker] caught signal %s, shutting down", signum)
        stop["flag"] = True

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    logger.info("[worker] starting poll loop (every %.1fs)", poll_seconds)
    while not stop["flag"]:
        try:
            tick_once()
        except Exception:
            logger.exception("[worker] tick crashed; will retry next interval")
        # Sleep in small slices so SIGINT is responsive.
        slept = 0.0
        while slept < poll_seconds and not stop["flag"]:
            time.sleep(min(0.5, poll_seconds - slept))
            slept += 0.5
    logger.info("[worker] exited cleanly")


def main() -> None:
    parser = argparse.ArgumentParser(description="applyd apply worker")
    parser.add_argument(
        "--poll-seconds", type=float, default=30.0, help="poll interval in seconds"
    )
    parser.add_argument(
        "--log-level", default="INFO", help="python logging level (DEBUG/INFO/WARNING)"
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run_forever(poll_seconds=args.poll_seconds)


if __name__ == "__main__":
    main()
