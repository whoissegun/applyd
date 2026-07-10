-- Spend aggregation views over usage_events.
--
-- usage_events is the granular ledger (one row per cost-incurring op); these
-- views are the "how much are we burning" read layer for dashboards and
-- ad-hoc audits. security_invoker so RLS on the base table applies to the
-- caller: authenticated users see only their own rows, the service key sees
-- everything.

create or replace view public.usage_daily
with (security_invoker = on) as
select
  user_id,
  date_trunc('day', occurred_at)::date as day,
  event_type,
  count(*)                             as events,
  sum(cost_cents)                      as cost_cents,
  round(sum(cost_cents) / 100.0, 2)    as cost_usd
from public.usage_events
group by user_id, date_trunc('day', occurred_at)::date, event_type;

create or replace view public.usage_monthly
with (security_invoker = on) as
select
  user_id,
  date_trunc('month', occurred_at)::date as month,
  event_type,
  count(*)                               as events,
  sum(cost_cents)                        as cost_cents,
  round(sum(cost_cents) / 100.0, 2)      as cost_usd
from public.usage_events
group by user_id, date_trunc('month', occurred_at)::date, event_type;

-- Cost per apply OUTCOME: joins each apply usage row to its attempt so spend
-- can be split into "bought an application" vs "burned on fails/skips".
create or replace view public.apply_spend_by_outcome
with (security_invoker = on) as
select
  ue.user_id,
  date_trunc('day', ue.occurred_at)::date as day,
  coalesce(aa.status, 'unknown')          as attempt_status,
  count(*)                                as runs,
  sum(ue.cost_cents)                      as cost_cents,
  round(avg(ue.cost_cents), 1)            as avg_cost_cents
from public.usage_events ue
left join public.apply_attempts aa
  on aa.id = (ue.metadata ->> 'attempt_id')::uuid
where ue.event_type = 'apply'
group by ue.user_id, date_trunc('day', ue.occurred_at)::date, coalesce(aa.status, 'unknown');

-- Data API exposure (2026-04-28 breaking change: no auto-grants in public).
grant select on public.usage_daily to authenticated;
grant select on public.usage_monthly to authenticated;
grant select on public.apply_spend_by_outcome to authenticated;
