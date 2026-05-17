# Deploy (Railway)

One repo, one `Dockerfile`. For the current no-users / private-beta phase, run
two Railway services:

| Service      | Type   | Config file                 |
|--------------|--------|-----------------------------|
| `api`        | web    | `railway/api.toml`          |
| `worker-all` | worker | `railway/worker-all.toml`   |

`discover`, `enrich`, and `matchmaker` are manual commands for now. Run them
from a developer machine or one-off Railway jobs when you want to refresh and
score the catalog.

Historical split-service configs still exist under `railway/<service>.toml`.
Point each Railway service's
**Settings → Config-as-Code Path** at the matching file — Railway picks up the
start command (and cron schedule, for the two crons).

| Optional later service | Type      | Config file                       |
|------------------------|-----------|-----------------------------------|
| `worker-tailor`        | worker    | `railway/worker-tailor.toml`      |
| `worker-apply`         | worker    | `railway/worker-apply.toml`       |
| `worker-matchmaker`    | worker    | `railway/worker-matchmaker.toml`  |
| `cron-discover`        | cron (6h) | `railway/cron-discover.toml`      |
| `cron-enrich`          | cron (6h) | `railway/cron-enrich.toml`        |

The root `railway.toml` defines the shared build (`Dockerfile`) and a default
start command (the API), so a fresh Railway service pointing at the repo with
no Config-as-Code Path set still boots as the API.

Web (`api`) listens on `$PORT` (Railway injects it). Workers and crons don't
bind a port.

## Shared environment

Set these on every service (Railway → Variables → Shared, or per-service):

```
SUPABASE_URL=
SUPABASE_SECRET_KEY=
ANTHROPIC_API_KEY=
OPENROUTER_API_KEY=
BRAVE_SEARCH_API_KEY=
SPIDER_API_KEY=
BRIGHTDATA_CUSTOMER_ID=
BRIGHTDATA_ZONE=
BRIGHTDATA_ZONE_PASSWORD=
BRIGHTDATA_HOST=
BRIGHTDATA_CDP_PORT=
APPLYD_TEST_MODE=true
APPLYD_TAILOR_BATCH=10
APPLYD_APPLY_BATCH=10
APPLYD_WORKER_IDLE_SLEEP_SECONDS=30
```

Only `worker-apply` strictly needs the `BRIGHTDATA_*` set; only the cron jobs
+ matchmaker need `BRAVE_SEARCH_API_KEY` / `SPIDER_API_KEY`. Setting them
everywhere is cheaper than tracking which service needs what — they're cheap
secrets, not per-service state.

Keep `APPLYD_TEST_MODE=true` until you've watched the apply worker fill a few
forms in production. Flip to `false` once confident; submissions are irrevocable.

## Database

Migrations are applied with the `supabase` CLI from a developer machine
(linked to the project), **not** from inside the image — Railway only runs the
service. Run `supabase db push` after merging schema changes.

## Logs

Workers emit one line per claim/release. The apply worker also prints turn
counts and per-tool call counts on failure. Railway's log retention is
24h–7d depending on plan; ship to a long-term sink later (Logflare,
BetterStack) if any of these get noisy.

## Manual catalog run

For now:

```
python scripts/manual_pipeline.py all --enrich-workers 8 --score-workers 4
```

The always-on `worker-all` service will then tailor pending applications and
apply to tailored applications.

## Scaling notes

- One `worker-apply` instance ≈ 1 concurrent Bright Data session. Bright Data
  bills per GB; per-instance memory is small (~150MB). Add replicas to
  parallelize, not bigger instances.
- One `worker-tailor` instance handles many tailors in parallel — work is
  bound by Claude API latency, not local CPU. One replica is usually plenty
  until applications/min outpaces it.
- `cron-discover` and `cron-enrich` are idempotent; safe to re-run if a cron
  fires late or doubles up.
- `worker-all` is intentionally sequential: up to 10 tailors, then up to 10
  apply attempts, then repeat. Split apply back out first if browser sessions
  start starving tailoring.
