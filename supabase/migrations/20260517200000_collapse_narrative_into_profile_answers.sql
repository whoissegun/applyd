-- Collapse all narrative profile fields into the single profile_answers blob.
-- Data was already migrated by scripts/backfill_profile_answers.py (one-shot
-- read of existing rows, labeled concatenation into profile_answers).
--
-- After this migration:
--   - profile_answers is the sole narrative bank
--   - structured columns remain only for literal form-fill values
--     (full_name, phone, linkedin_url, github_url, portfolio_url)

alter table public.user_profiles
  drop column if exists target_roles,
  drop column if exists work_auth_summary,
  drop column if exists sponsorship_needed_countries,
  drop column if exists target_levels,
  drop column if exists target_specialties,
  drop column if exists target_locations;
