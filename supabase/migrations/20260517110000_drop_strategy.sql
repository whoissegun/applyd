-- Drop volume/precision strategy column. Replaced by target_roles +
-- profile_answers as the matcher's input.

alter table public.user_profiles drop column if exists strategy;
