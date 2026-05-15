-- Telemetry for apply runner efficiency. Both nullable — old rows + first
-- migration window stay NULL until apply_for_user is patched to populate them.
--
-- turn_count: how many tool-use iterations the agent took to reach a terminal
-- state. Drives cost optimization (each turn ≈ $0.012; cutting 20→12 saves
-- ~33% of the LLM bucket).
--
-- tool_call_counts: per-tool histogram for THE same attempt. Lets us see if
-- the agent is over-snapshotting, over-navigating, etc.
--   example: {"navigate": 1, "snapshot": 4, "fill_many": 2, "click_many": 8,
--             "submit": 1, "report_done": 1}

alter table public.apply_attempts
  add column turn_count       integer,
  add column tool_call_counts jsonb;

create index apply_attempts_turn_count_idx
  on public.apply_attempts (turn_count)
  where turn_count is not null;
