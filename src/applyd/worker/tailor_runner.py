"""Tailor worker: polls public.applications for 'pending' rows, runs the
multi-tenant tailor pipeline on each, and moves them to 'tailored' (or
'failed').

Mirrors the shape of the apply worker (runner.py). Same SIGINT/SIGTERM
handling, same poll loop, same tick_once() factoring so tests can drive
a single iteration without spinning up the loop.

Entry: python -m applyd.worker.tailor_runner [--poll-seconds 30]
"""

from __future__ import annotations

import argparse
import logging
import signal
import time
from typing import Optional

from applyd.config import load_env

load_env()

from applyd.db import get_client  # noqa: E402
from applyd.tailor.saas import tailor_for_user  # noqa: E402

logger = logging.getLogger("applyd.worker.tailor")


def _find_one_pending() -> Optional[dict]:
    sb = get_client()
    res = (
        sb.table("applications")
        .select("id, user_id, job_id")
        .eq("status", "pending")
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def tick_once() -> Optional[dict]:
    """One iteration of the polling loop, factored for tests.

    Returns the tailor_for_user result dict, or None if nothing pending.
    """
    candidate = _find_one_pending()
    if candidate is None:
        return None

    user_id = candidate["user_id"]
    job_id = candidate["job_id"]
    app_id = candidate["id"]

    logger.info(
        "[tailor-worker] picked app %s (user=%s job=%s)", app_id, user_id, job_id
    )
    print(f"[tailor-worker] picked app {app_id} (user={user_id} job={job_id})")

    try:
        result = tailor_for_user(user_id, job_id)
    except Exception as exc:  # noqa: BLE001 — surfaced from saas.tailor_for_user
        logger.exception("[tailor-worker] tailor_for_user crashed: %s", exc)
        return {"status": "crashed", "application_id": app_id, "error": str(exc)}

    logger.info(
        "[tailor-worker] result for app %s: %s", app_id, result.get("status")
    )
    return result


def run_forever(poll_seconds: float = 30.0) -> None:
    stop = {"flag": False}

    def _handle(signum, _frame):  # noqa: ANN001
        logger.info("[tailor-worker] caught signal %s, shutting down", signum)
        stop["flag"] = True

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    logger.info("[tailor-worker] starting poll loop (every %.1fs)", poll_seconds)
    while not stop["flag"]:
        try:
            tick_once()
        except Exception:
            logger.exception(
                "[tailor-worker] tick crashed; will retry next interval"
            )
        slept = 0.0
        while slept < poll_seconds and not stop["flag"]:
            time.sleep(min(0.5, poll_seconds - slept))
            slept += 0.5
    logger.info("[tailor-worker] exited cleanly")


def main() -> None:
    parser = argparse.ArgumentParser(description="applyd tailor worker")
    parser.add_argument(
        "--poll-seconds", type=float, default=30.0, help="poll interval in seconds"
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="python logging level (DEBUG/INFO/WARNING)",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run_forever(poll_seconds=args.poll_seconds)


if __name__ == "__main__":
    main()
