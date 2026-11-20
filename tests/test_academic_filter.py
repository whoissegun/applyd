from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from applyd.commands.apply import cmd_apply
from applyd.commands.apply_batch import cmd_apply_batch
from applyd.eligibility import academic_policy_reasons, evaluate_job
from applyd.enrichment.extract import normalize_facts
from applyd.models import Job


class AcademicFilterTests(unittest.TestCase):
    profile = {'academic_status': {'level':'bachelors', 'status':'in_progress'},
               'preferences': {'excluded_required_education':['masters', 'doctorate']}}

    def role(self, description):
        now = datetime.now(timezone.utc)
        return Job(id='test', source='test', external_id='test', company='Example',
                   title='Engineer', url='https://example.com/job', description=description,
                   first_seen_at=now, last_seen_at=now)

    def facts(self, value, evidence, description=None):
        return normalize_facts({'education': {'value':value, 'evidence':evidence}},
                               title='Engineer', description=description or evidence)

    def test_required_advanced_degrees_and_enrollment_are_excluded(self):
        cases = [
            ('masters_required', "Master's degree in computer science required."),
            ('masters_required', 'M.S. or Ph.D. in Electrical Engineering, preferably from a top-tier university.'),
            ('masters_required', "Currently enrolled in a master's program."),
            ('doctorate_required', 'Ph.D. in computer science required.'),
            ('doctorate_required', 'Currently enrolled in a PhD program.'),
            ('masters_required', "Master's required; PhD preferred."),
            ('masters_required', "Bachelor's and master's degrees required."),
            ('masters_required', 'MS in computer science required.'),
        ]
        for value, evidence in cases:
            with self.subTest(evidence=evidence):
                result = evaluate_job(self.role(evidence), self.facts(value, evidence), self.profile)
                self.assertEqual(result.decision, 'ineligible')
                self.assertEqual(result.reasons[0]['code'], 'required_education_excluded')
                self.assertEqual(result.reasons[0]['job_evidence'], evidence)

    def test_preferred_alternatives_and_unknowns_do_not_block(self):
        cases = [
            ('masters_required', "Master's degree preferred."),
            ('doctorate_required', 'PhD is a plus.'),
            ('doctorate_required', 'Preferred qualifications: PhD in computer science.'),
            ('masters_required', "Bachelor's or master's degree in computer science."),
            ('masters_required', "Bachelor's degree required; master's degree preferred."),
            ('masters_required', 'BS/MS in computer science.'),
            ('doctorate_required', "Bachelor's required; master's or PhD preferred."),
            ('masters_required', "Master's degree or equivalent practical experience."),
            ('doctorate_required', 'PhD student, recent graduate, or early-career researcher/engineer in Computer Science.'),
            ('none_stated', 'No degree requirement stated.'),
        ]
        for value, evidence in cases:
            with self.subTest(evidence=evidence):
                self.assertEqual(evaluate_job(self.role(evidence), self.facts(value, evidence),
                                             self.profile).decision, 'eligible')

    def test_shortened_quote_cannot_hide_preference(self):
        description = "Master's degree preferred. Build useful software."
        facts = self.facts('masters_required', "Master's degree", description)
        self.assertEqual(evaluate_job(self.role(description), facts, self.profile).decision, 'eligible')

    def test_grounded_flag_alone_cannot_block_or_override_opt_in(self):
        role = self.role('No educational requirement stated.')
        facts = {'education': {'value':'doctorate_required', 'evidence':'PhD required',
                               'evidence_grounded':True}}
        self.assertEqual(academic_policy_reasons(role, facts, self.profile), [])
        role = self.role('PhD required')
        self.assertEqual(academic_policy_reasons(role, facts, {}), [])

    def test_direct_apply_blocks_before_browser_or_claim(self):
        evidence = 'PhD required'
        store = Mock()
        store.get.return_value = self.role(evidence)
        store.get_profile.return_value = (self.profile, 'profile_hash')
        store.get_facts.return_value = self.facts('doctorate_required', evidence)
        with patch('applyd.commands.apply.get_local_store', return_value=store), \
             patch('applyd.commands.apply.check_jobs_liveness') as live:
            self.assertEqual(cmd_apply(SimpleNamespace(job_id='test')), 1)
        live.assert_not_called()
        store.start_apply_attempt.assert_not_called()
        store.set_evaluation.assert_called_once()

    def test_batch_rechecks_academic_filter_despite_stale_eligible_ranking(self):
        evidence = "Master's degree required."
        store = Mock()
        store.get_profile.return_value = (self.profile, 'profile_hash')
        store.iter_ranked_matches.return_value = iter([{
            'job_id':'test', 'company':'Example', 'title':'Engineer', 'score':90,
        }])
        store.iter_prior_application_jobs.return_value = iter([])
        store.get_evaluation.return_value = {'decision':'eligible'}
        store.get_apply_attempts.return_value = []
        store.get_application_by_job.return_value = None
        store.get.return_value = self.role(evidence)
        store.get_facts.return_value = self.facts('masters_required', evidence)
        with TemporaryDirectory() as folder:
            args = SimpleNamespace(top=1, minimum_score=0, test_mode=False,
                captcha_fallback='none', max_total_apply_cost_usd=0,
                report=str(Path(folder) / 'report.json'))
            with patch('applyd.commands.apply_batch.get_local_store', return_value=store), \
                 patch('applyd.commands.apply_batch.check_jobs_liveness') as live, \
                 patch('applyd.commands.apply_batch.cmd_apply') as apply:
                self.assertEqual(cmd_apply_batch(args), 2)
        live.assert_not_called()
        apply.assert_not_called()
        store.set_evaluation.assert_called_once()


if __name__ == '__main__':
    unittest.main()
