-- Freeform target-roles statement, LLM-prefilled at onboarding, user-editable.
-- Replaces the enum-style target_levels/target_specialties as the source of
-- truth for "what is this user looking for" — those columns stay for now
-- but the matchmaker should prefer target_roles when populated.

alter table public.user_profiles
  add column if not exists target_roles text;
