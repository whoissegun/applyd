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
import os
import random
import signal
import time
from datetime import datetime, timezone
from typing import Optional

from applyd.config import load_env

load_env()

from applyd.apply.saas import apply_for_user  # noqa: E402
from applyd.db import get_client  # noqa: E402
from applyd.llm_errors import TransientInfraError  # noqa: E402

logger = logging.getLogger("applyd.worker")

INFRA_BACKOFF_SECONDS = 120.0  # back-off after a transient (no-credits/rate-limit) error

# Only freshly-tailored rows are claimable. 'failed' is terminal — a previous
# attempt already burned cost and wrote an apply_attempts audit row. To retry
# manually, flip the row back to 'tailored' in the DB.
CLAIMABLE = ("tailored",)

# Anti-burst pacing. Applications from one identity arriving in a tight, regular
# cadence are a spam/fraud signal to ATS reCAPTCHA + blocklists (the reason we
# were getting email-verification challenges). Behavioral realism in the browser
# is the primary fix; these are cheap insurance on top.
#   - jitter: a randomized gap after each apply so the cadence isn't a metronome
#     (applies already take ~95s; this varies it, it doesn't add meaningful idle)
#   - daily cap: a hard ceiling on applies/day as a runaway backstop
# Both are env-tunable; the cap defaults to 0 (unlimited) to preserve the
# volume-first strategy until a number is chosen.
_APPLY_JITTER_MIN_S = float(os.environ.get("APPLYD_APPLY_JITTER_MIN_SECONDS", "8"))
_APPLY_JITTER_MAX_S = float(os.environ.get("APPLYD_APPLY_JITTER_MAX_SECONDS", "25"))
_APPLY_DAILY_CAP = int(os.environ.get("APPLYD_APPLY_DAILY_CAP", "0"))


def _applied_today() -> int:
    """Count applications that reached 'applied' since midnight UTC. Cheap single
    indexed count; used only when a daily cap is configured."""
    sb = get_client()
    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    res = (
        sb.table("apply_attempts")
        .select("id", count="exact")
        .eq("status", "applied")
        .gte("ended_at", midnight.isoformat())
        .limit(0)
        .execute()
    )
    return res.count or 0


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
    if _APPLY_DAILY_CAP > 0:
        done = _applied_today()
        if done >= _APPLY_DAILY_CAP:
            logger.info("[worker] daily apply cap reached (%d/%d) — pausing applies", done, _APPLY_DAILY_CAP)
            return None

    candidate = _find_one_claimable()
    if candidate is None:
        return None

    app_id = candidate["id"]
    user_id = candidate["user_id"]

    logger.info("[worker] dispatching app %s for user %s", app_id, user_id)
    print(f"[worker] dispatching app {app_id} for user {user_id}")

    try:
        result = apply_for_user(user_id=user_id, application_id=app_id)
    except TransientInfraError:
        # Account/provider-wide failure; the row was requeued (not failed).
        # Propagate so the loop / worker-all backs off instead of hammering on.
        raise
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
    # Jitter after a real dispatch (any terminal outcome touched the ATS) so the
    # apply cadence isn't a fixed metronome. Skipped for lost races / no-ops.
    if _APPLY_JITTER_MAX_S > 0 and result.get("status") in {"applied", "skipped", "failed"}:
        time.sleep(random.uniform(min(_APPLY_JITTER_MIN_S, _APPLY_JITTER_MAX_S), _APPLY_JITTER_MAX_S))
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
        sleep_for = poll_seconds
        try:
            tick_once()
        except TransientInfraError as exc:
            # Requeued already; back off longer than the normal poll so we're not
            # hammering an empty balance / rate limit.
            logger.warning("[worker] infra backoff after transient error: %s", exc)
            sleep_for = max(poll_seconds, INFRA_BACKOFF_SECONDS)
        except Exception:
            logger.exception("[worker] tick crashed; will retry next interval")
        # Sleep in small slices so SIGINT is responsive.
        slept = 0.0
        while slept < sleep_for and not stop["flag"]:
            time.sleep(min(0.5, sleep_for - slept))
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
