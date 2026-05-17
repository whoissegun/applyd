"""Combined worker for early-stage deployments.

Runs the two user-facing background stages in one process:

  1. tailor up to N pending applications
  2. apply to up to N tailored applications

Discovery, enrichment, and matchmaker are intentionally left manual for now.
The underlying stages still use Supabase status transitions and atomic claims,
so this can be split back into separate Railway services later without a data
migration.

Entry: python -m applyd.worker.all
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import time
from collections.abc import Callable
from typing import Any

from applyd.config import load_env

load_env()

from applyd.worker import runner as apply_runner  # noqa: E402
from applyd.worker import tailor_runner  # noqa: E402

logger = logging.getLogger("applyd.worker.all")
_stop = False


def _on_signal(signum: int, _frame: Any) -> None:
    global _stop
    _stop = True
    logger.info("[worker-all] caught signal %s, stopping after current job", signum)


def _run_batch(
    *,
    name: str,
    tick: Callable[[], dict | None],
    limit: int,
) -> int:
    """Run one worker tick until there is no work or the batch limit is hit."""
    completed = 0
    for _ in range(limit):
        if _stop:
            break
        try:
            result = tick()
        except Exception:
            logger.exception("[worker-all] %s tick crashed", name)
            break
        if result is None:
            break
        completed += 1
        logger.info(
            "[worker-all] %s result: status=%s application_id=%s",
            name,
            result.get("status"),
            result.get("application_id"),
        )
    return completed


def run_forever(
    *,
    tailor_batch: int = 10,
    apply_batch: int = 10,
    idle_sleep_seconds: float = 30.0,
) -> None:
    """Alternate between bounded tailor and apply batches forever."""
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    logger.info(
        "[worker-all] starting tailor_batch=%d apply_batch=%d idle_sleep=%.1fs",
        tailor_batch,
        apply_batch,
        idle_sleep_seconds,
    )

    while not _stop:
        tailored = _run_batch(
            name="tailor",
            tick=tailor_runner.tick_once,
            limit=tailor_batch,
        )
        applied = _run_batch(
            name="apply",
            tick=apply_runner.tick_once,
            limit=apply_batch,
        )

        logger.info(
            "[worker-all] cycle complete: tailored=%d applied_or_terminal=%d",
            tailored,
            applied,
        )

        if tailored == 0 and applied == 0 and not _stop:
            slept = 0.0
            while slept < idle_sleep_seconds and not _stop:
                step = min(0.5, idle_sleep_seconds - slept)
                time.sleep(step)
                slept += step

    logger.info("[worker-all] exited cleanly")


def main() -> None:
    parser = argparse.ArgumentParser(description="applyd combined worker")
    parser.add_argument(
        "--tailor-batch",
        type=int,
        default=int(os.environ.get("APPLYD_TAILOR_BATCH", "10")),
        help="max pending applications to tailor per cycle",
    )
    parser.add_argument(
        "--apply-batch",
        type=int,
        default=int(os.environ.get("APPLYD_APPLY_BATCH", "10")),
        help="max tailored applications to apply per cycle",
    )
    parser.add_argument(
        "--idle-sleep-seconds",
        type=float,
        default=float(os.environ.get("APPLYD_WORKER_IDLE_SLEEP_SECONDS", "30")),
        help="sleep duration when both queues are empty",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("APPLYD_WORKER_LOG_LEVEL", "INFO"),
        help="python logging level (DEBUG/INFO/WARNING)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run_forever(
        tailor_batch=max(1, args.tailor_batch),
        apply_batch=max(1, args.apply_batch),
        idle_sleep_seconds=max(1.0, args.idle_sleep_seconds),
    )


if __name__ == "__main__":
    main()
