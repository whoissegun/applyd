-- Operational failure category on applications, set alongside the freeform
-- last_error whenever a row is released to a terminal/requeued state. Lets us
-- aggregate failures by type (compile vs llm_infra vs dead_link vs …) and drives
-- the systemic-failure detector, instead of regexing freeform text. See
-- applyd.failures for the taxonomy. Column inherits the table's RLS + grants.

alter table public.applications
  add column if not exists failure_category text;

create index if not exists applications_failure_category_idx
  on public.applications (failure_category)
  where failure_category is not null;
