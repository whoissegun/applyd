import importlib.util
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location('application_report',
    Path(__file__).resolve().parents[1] / 'scripts/report_ingested_applications.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ApplicationAuditReportTests(unittest.TestCase):
    def test_markdown_uses_current_batch_scope(self):
        report = dict(scope='100 jobs ingested 2026-10-25', generated_at='now',
                      counts={}, review_categories={}, model_cost_usd=0,
                      jobs=[], notes=[])
        rendered = module.markdown_report(report)
        self.assertIn('100 jobs ingested 2026-10-25', rendered)
        self.assertNotIn('October 22', rendered)

    def test_correction_receipt_does_not_claim_application_was_corrected(self):
        job = dict(company='Sim', title='Engineer', url='https://example.test', status='applied',
                   answer_quality_flags=[dict(question='Visa required?', entered='No', correct_answer='Yes')],
                   correction_requests=[dict(status='request_received', acknowledgement='Message received')])
        report = dict(scope='test batch', generated_at='now', counts={'applied': 1},
                      model_cost_usd=0, jobs=[job], notes=[])
        rendered = module.markdown_report(report)
        self.assertIn('Message received', rendered)
        self.assertIn('ATS answer update remains unconfirmed', rendered)
        self.assertIn('Entered: No', rendered)

    def test_attachment_uncertainty_is_separate_from_answer_error(self):
        job = dict(company='Mustard', title='Trader', url='https://example.test', status='applied',
                   submission_artifact_flags=[dict(issue='Resume attachment unverified; do not resubmit automatically.')])
        report = dict(scope='test batch', generated_at='now', counts={'applied': 1},
                      model_cost_usd=0, jobs=[job], notes=[])
        rendered = module.markdown_report(report)
        self.assertIn('attachment check needed', rendered)
        self.assertIn('Resume attachment unverified', rendered)
        self.assertNotIn('Submitted answer needs review', rendered)

    def test_exact_missing_facts_are_not_mislabeled_as_code_failures(self):
        job = {'reason':'review:jev_decision', 'review_details':[{'question':'Do you have any upcoming offer deadlines?'}]}
        self.assertEqual(module.review_triage(job)[0], 'user_fact_or_access')
        job['review_details'] = [{'question':'Please select your right to work status'}]
        self.assertEqual(module.review_triage(job)[0], 'legal_status_category')
        job['review_details'] = [{'error':'exact graduation day is not grounded in the month-only profile; submission not attempted'}]
        self.assertEqual(module.review_triage(job)[0], 'profile_date_precision')
        job['review_details'] = [{'question':'Have you previously worked at a supplier market research firm?'}]
        self.assertEqual(module.review_triage(job)[0], 'user_fact_or_access')
        job = {'reason': 'review:repeated_tool_failure | tool=submit',
               'review_details': [{'error': 'validation errors remain: referral source'}]}
        self.assertEqual(module.review_triage(job)[0], 'code_or_mapping')

    def test_submitted_answer_quality_flags_are_kept_in_review_details(self):
        _, issues = module.answers_from_trace([dict(event_type='answer_quality_flag',
            name=None, payload={'question':'Graduation date', 'entered':'04/01/2027',
                                'grounded_profile':'2027-04'}, turn=None)])
        self.assertEqual(issues[0]['entered'], '04/01/2027')

    def test_review_triage_keeps_uncertain_submission_separate_from_code_fixes(self):
        category, action = module.review_triage({'reason':'review:submission_unconfirmed'})
        self.assertEqual(category, 'submission_uncertain')
        self.assertIn('Do not retry automatically', action)
        self.assertEqual(module.review_triage({'reason':'gated:captcha'})[0], 'manual_captcha')
        self.assertEqual(module.review_triage({'reason':'review:jev_required'})[0], 'code_or_mapping')

    def test_upload_refresh_updates_reference_to_question_mapping(self):
        def event(kind, name, payload):
            return dict(event_type=kind, name=name, payload=payload, turn=1)
        events = [
            event('tool_result', 'snapshot', {'result':"r1: [combobox/text *] 'Old question'"}),
            event('tool_result', 'upload_resume', {'result':
                "ok: uploaded\nDOM refreshed:\nr1: [combobox/text *] 'New question'"}),
            event('tool_call', 'select_option', {'args': {'ref':'r1', 'value':'No'}}),
            event('tool_result', 'select_option', {'result':"ok: selected 'No'"}),
        ]
        answers, _ = module.answers_from_trace(events)
        self.assertEqual(answers[0]['question'], 'New question')

    def test_records_only_successful_choices_and_omits_identity(self):
        def event(kind, name, payload):
            return dict(event_type=kind, name=name, payload=payload, turn=1)
        events = [
            event('tool_result', 'snapshot', {'result':
                "r1: [combobox/text *] 'Are you authorized to work in the United States?'\n"
                "r2: [input/text] 'Name'\n"
                "r3: [button *] 'Are you authorized to work in Canada? — Yes'"}),
            event('tool_call', 'select_option', {'args': {'ref':'r1', 'value':'Yes'}}),
            event('tool_result', 'select_option', {'result':'error: blocked'}),
            event('tool_call', 'select_option', {'args': {'ref':'r1', 'value':'No'}}),
            event('tool_result', 'select_option', {'result':"ok: selected 'No'"}),
            event('tool_call', 'fill', {'args': {'ref':'r2', 'value':'Private Person'}}),
            event('tool_result', 'fill', {'result':'ok: filled'}),
            event('tool_call', 'click', {'args': {'ref':'r3'}}),
            event('tool_result', 'click', {'result':'ok: clicked r3'}),
        ]
        answers, issues = module.answers_from_trace(events)
        self.assertEqual([(a['question'], a['answer']) for a in answers], [
            ('Are you authorized to work in the United States?', 'No'),
            ('Are you authorized to work in Canada?', 'Yes'),
        ])
        self.assertEqual(len(issues), 1)


if __name__ == '__main__':
    unittest.main()
