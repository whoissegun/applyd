-- LLM-driven filtering replaces structured enums.
--
-- jobs.classification: free-text-but-structured output from a Haiku classifier
-- run once per job (at enrichment time, or via backfill for the existing 7,714
-- rows). Schema is intentionally open — the matcher works against whatever
-- keys are present. Typical shape:
--   {
--     "role_summary": "...",
--     "seniority_signal": "...",
--     "domain_focus": ["...", "..."],
--     "tech_stack_required": ["..."],
--     "deal_breakers": ["..."],
--     "soft_signals": ["..."]
--   }
--
-- agent_wall_seconds: pure agent-loop time on an apply attempt (LLM + browser),
-- distinct from started_at→ended_at which includes setup overhead.

alter table public.jobs
  add column classification jsonb;

alter table public.apply_attempts
  add column agent_wall_seconds integer;

-- Index for the matcher's "give me classified jobs we haven't matched against
-- this user yet" query.
create index jobs_classification_idx
  on public.jobs (id)
  where classification is not null;
