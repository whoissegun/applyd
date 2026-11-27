"""Offline regressions from failed application traces; no submissions or API calls."""
import json
import os
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from applyd.apply import tools
from applyd.apply.runner import run_apply, _terminal_tool_verdict, _repeated_failure_note
from applyd.commands.apply_batch import _failure_category, _ats_failure_total
from collections import Counter


class RecoveryTests(unittest.TestCase):
    def test_visa_requirement_guard_blocks_the_observed_false_no(self):
        question = 'Do you require any visas or work authorizations to work in California?'
        profile = {'work_authorization': {'US': {'authorized': False, 'requires_sponsorship': True}}}
        loc = MagicMock()
        loc.get_attribute.side_effect = lambda key: {'data-applyd-question': question,
            'data-applyd-label': question, 'data-applyd-option': 'No'}.get(key)
        with patch.object(tools, '_ref_locator', return_value=loc):
            self.assertIn('unsupported answer blocked', tools._profile_click_guard(MagicMock(), 'r1', profile, ['SF']))
            self.assertIn('unsupported answer blocked', tools._select_profile_guard(MagicMock(), 'r1', 'No', profile, '', ['SF']))
            self.assertIsNone(tools._select_profile_guard(MagicMock(), 'r1', 'Yes', profile, '', ['SF']))

    def test_in_progress_degree_claim_is_blocked_for_clicks_and_selects(self):
        profile = {'degree': 'Bachelor of Computer Science',
                   'academic_status': {'level': 'bachelors', 'status': 'in_progress'}}
        question = 'Do you have a bachelor’s or master’s in Computer Science?'
        loc = MagicMock()
        loc.get_attribute.side_effect = lambda key: {
            'data-applyd-question': question, 'data-applyd-label': question,
            'data-applyd-option': 'Yes'}.get(key)
        with patch.object(tools, '_ref_locator', return_value=loc):
            self.assertIn('credential completion', tools._profile_click_guard(MagicMock(), 'r1', profile))
            self.assertIn('credential completion', tools._select_profile_guard(MagicMock(), 'r1', 'Yes', profile, '', []))
            self.assertIsNone(tools._select_profile_guard(MagicMock(), 'r1', 'No', profile, '', []))

    def test_observed_arbitration_checkbox_cannot_be_accepted_by_model(self):
        loc=MagicMock()
        loc.get_attribute.side_effect=lambda name: {
            'data-applyd-label':'Applicant Arbitration Agreement Acknowledgement — I acknowledge that I have read and understood',
            'data-applyd-question':'Applicant Arbitration Agreement Acknowledgement',
            'data-applyd-option':'I acknowledge',
        }.get(name,'')
        profile={'application_policy':{'ordinary_accuracy_attestation':'authorized','unusual_legal_attestation':'review'}}
        with patch.object(tools,'_ref_locator',return_value=loc):
            self.assertIn('human review',tools._profile_click_guard(MagicMock(),'r1',profile))
            self.assertIn('human review',tools._select_profile_guard(MagicMock(),'r1','I acknowledge',profile,'',[]))

    def test_ashby_spam_rejection_stops_without_retry_or_false_success(self):
        verdict=_terminal_tool_verdict('submit',
            'error: submit r13: Your application submission was flagged as possible spam; application not confirmed')
        self.assertEqual(verdict[0],'review')
        self.assertTrue(verdict[1].startswith('gated:spam'))
        self.assertIsNone(_terminal_tool_verdict('submit','ok: submission_confirmed'))

    def test_known_city_location_uses_matching_sponsorship_guard(self):
        loc = MagicMock()
        loc.get_attribute.return_value = 'Will you require work sponsorship now and/or in the future?'
        profile = {'work_authorization': {
            'US': {'authorized': False, 'requires_sponsorship': True},
            'CA': {'authorized': True, 'requires_sponsorship': False},
        }}
        with patch.object(tools, '_ref_locator', return_value=loc):
            self.assertIsNone(tools._select_profile_guard(
                MagicMock(), 'r13', 'Yes', profile, '', ['San Francisco'],
            ))
            result = tools._select_profile_guard(
                MagicMock(), 'r13', 'No', profile, '', ['San Francisco'],
            )
            self.assertIn('refused false US no-sponsorship', result)
            result = tools._select_profile_guard(MagicMock(), 'r13', 'No',
                                                profile, '', ['United States'])
            self.assertIn('refused false US no-sponsorship', result)
            result = tools._select_profile_guard(MagicMock(), 'r13', 'Yes',
                                                {'work_authorization': {'US': {'authorized': False}}},
                                                '', ['United States'])
            self.assertIn('legal answer blocked', result)

    def test_sponsorship_negation_and_contradictions(self):
        loc = MagicMock()
        loc.get_attribute.return_value = (
            'Do you now or will you in the future require sponsorship '
            'or employment authorization to work in Canada?'
        )
        cases = [
            ('No, I do not / will not require sponsorship', False, False),
            ('No, I do not / will not require sponsorship', True, True),
            ('Yes, I require sponsorship', False, True),
            ('Yes, I require sponsorship', True, False),
            ('I do not need sponsorship', False, False),
            ('No sponsorship now but I will require sponsorship later', False, True),
        ]
        with patch.object(tools, '_ref_locator', return_value=loc):
            for value, requires, blocked in cases:
                with self.subTest(value=value, requires=requires):
                    result = tools._select_profile_guard(
                        MagicMock(), 'r1', value,
                        {'work_authorization': {'CA': {
                            'authorized': True, 'requires_sponsorship': requires,
                        }}}, '', ['Toronto, Canada'],
                    )
                    self.assertEqual(result is not None, blocked, result)

    def test_graduation_month_is_grounded_and_field_scoped(self):
        profile = {'expected_grad_date': '2030-05'}
        self.assertIsNone(tools._date_profile_guard('Graduation month', 'May', profile))
        for label, value in [('Graduation month', 'April'),
                             ('Graduation year', 'May'),
                             ('Graduation date', 'May')]:
            self.assertIsNotNone(tools._date_profile_guard(label, value, profile))
        self.assertIsNotNone(tools._date_profile_guard(
            'Graduation month', 'May', {'expected_grad_date': '2030'}))

    def test_other_dropdown_closed_before_click(self):
        page, loc = MagicMock(), MagicMock()
        loc.evaluate.side_effect = ['button', True, None]
        loc.get_attribute.return_value = None
        page.evaluate.side_effect = [None, [{'ref': 'o0', 'text': 'May'}]]
        calls = []
        page.keyboard.press.side_effect = lambda *_: calls.append('escape')
        loc.click.side_effect = lambda **_: calls.append('click')
        with patch.object(tools, '_ref_locator', return_value=loc):
            result = tools.open_dropdown(page, 'r2')
        self.assertTrue(result.startswith('opened'), result)
        self.assertEqual(calls, ['escape', 'click'])

    def test_submit_refresh_does_not_hide_original_error(self):
        page = MagicMock()
        page.wait_for_timeout.side_effect = RuntimeError('private transport detail')
        original = 'error: submit r9: TargetClosedError: Target page, context or browser has been closed'
        with patch.object(tools, 'submit', return_value=original) as submit:
            result = tools.dispatch(page, 'submit', {'ref': 'r9'},
                                    resume_pdf_path='unused.pdf', test_mode=False)
        self.assertTrue(result.startswith(original), result)
        self.assertNotIn('private transport detail', result)
        submit.assert_called_once()
        self.assertEqual(_terminal_tool_verdict('submit', result)[0], 'infra_error')

    def test_autocomplete_without_real_options_cannot_claim_success(self):
        page, loc = MagicMock(), MagicMock()
        loc.evaluate.return_value = False
        page.evaluate.return_value = []
        with patch.object(tools, '_ref_locator', return_value=loc):
            result = tools.fill_autocomplete(page, 'r1', 'Job board')
        self.assertTrue(result.startswith('error:'), result)
        self.assertIn('no selectable suggestion', result)
        loc.press.assert_not_called()

    def test_failure_categories_preserve_safety_budget(self):
        for reason in ['review:agent_stall | required preflight ignored twice',
                       'hit MAX_TURNS=25 without report_done']:
            self.assertEqual(_failure_category({'status': 'failed', 'reason': reason}), 'agent_stall')
        self.assertEqual(_failure_category({'status': 'infra_error', 'reason': 'infra:browser_closed'}), 'runtime_failure')
        self.assertEqual(_ats_failure_total(Counter({('ashby', 'agent_stall'): 3}), 'ashby'), 3)

    def test_repeated_control_failures_are_not_mislabeled_as_transport(self):
        for result, category in [
            ('error: select_option r1: refused false CA sponsorship claim', 'policy_guard'),
            ('error: select_option r1: no unambiguous match', 'agent_stall'),
            ('error: click r1: TimeoutError: intercepted pointer events', 'tool_failure'),
        ]:
            note = _repeated_failure_note('select_option', result)
            self.assertEqual(_failure_category({'status': 'review', 'reason': note}), category)

    def test_preflight_forced_and_ignored_calls_stop_early(self):
        def response(name, args=None):
            message = MagicMock()
            message.content = None
            message.tool_calls = [SimpleNamespace(id=name, function=SimpleNamespace(
                name=name, arguments=json.dumps(args or {})))]
            message.model_dump.return_value = {'role': 'assistant'}
            return SimpleNamespace(usage=SimpleNamespace(cost=0),
                                   choices=[SimpleNamespace(message=message)])

        client = MagicMock()
        client.chat.completions.create.side_effect = [
            response('navigate'), response('snapshot'),
            response('click', {'ref': 'r1'}), response('click', {'ref': 'r1'}),
        ]

        @contextmanager
        def browser(*args, **kwargs):
            yield MagicMock()

        with patch.dict(os.environ, {'OPENROUTER_API_KEY':'fake'}), \
             patch('applyd.apply.runner._make_client', return_value=client), \
             patch('applyd.apply.runner.browser_page', browser), \
             patch('applyd.apply.runner.dispatch', side_effect=['ok: loaded', 'r1: Apply']) as dispatch, \
             patch('applyd.apply.runner._start_hard_watchdog'), \
             patch('applyd.apply.runner.load_env'), \
             patch('applyd.apply.runner.build_code_reader', return_value=None):
            result = run_apply(job_id='test', company='Example', title='Engineer',
                               job_url='https://example.test/job', resume_pdf_path='unused.pdf',
                               profile_md='{}', test_mode=True)
        self.assertEqual(result['status'], 'review')
        self.assertIn('review:agent_stall', result['note'])
        self.assertEqual(dispatch.call_count, 2)
        for call in client.chat.completions.create.call_args_list[2:]:
            self.assertEqual(call.kwargs['tool_choice']['function']['name'], 'preflight')


if __name__ == '__main__':
    unittest.main()


class RequiredResumeGuardTests(unittest.TestCase):
    def test_only_literal_true_blocks_submit(self) -> None:
        from unittest.mock import MagicMock
        from applyd.apply.runner import _required_resume_input_empty
        for returned, expected in ((True, True), (False, False), (MagicMock(), False)):
            page = MagicMock()
            page.evaluate.return_value = returned
            self.assertIs(expected, _required_resume_input_empty(page))
        page = MagicMock()
        page.evaluate.side_effect = RuntimeError("detached")
        self.assertFalse(_required_resume_input_empty(page))


class ResumeGapLabelTests(unittest.TestCase):
    def test_resume_upload_labels_are_not_missing_facts(self) -> None:
        from applyd.apply.runner import _profile_already_answers
        for label in ("resume — Attach||Enter manually", "Resume Choose file", "Resume"):
            self.assertTrue(_profile_already_answers(label, {}, resume_text="resume"), label)
        for label in ("cover letter — Attach", "Transcript"):
            self.assertFalse(_profile_already_answers(label, {}, resume_text="resume"), label)


class SavedFormStateTests(unittest.TestCase):
    def test_clears_origin_storage_but_keeps_cookies(self) -> None:
        from unittest.mock import MagicMock
        from applyd.apply.tools import _clear_saved_form_state
        page = MagicMock()
        session = page.context.new_cdp_session.return_value
        _clear_saved_form_state(page, "https://jobs.example.com/apply/123?x=1")
        method, params = session.send.call_args.args
        self.assertEqual("Storage.clearDataForOrigin", method)
        self.assertEqual("https://jobs.example.com", params["origin"])
        self.assertNotIn("cookies", params["storageTypes"])
        self.assertIn("local_storage", params["storageTypes"])
