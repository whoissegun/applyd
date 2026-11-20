"""Offline controller safety tests, including a real local Chromium form."""
import json
import os
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from applyd.apply.jev_controller import JevController, inspect_candidates, INSPECT_JS
from applyd.apply.runner import run_apply


def control(ref='r0', label='Email', **kwargs):
    return dict(ref=ref, label=label, tag='input', type='email', role='',
                required=True, value='', **kwargs)


def form(*controls):
    return {'forms': 1, 'controls': list(controls) + [
        dict(ref='r99', label='Submit application', tag='button', type='submit',
             role='', text='Submit application')]}


def response(choice='email', confidence=.99):
    return MagicMock(status_code=200, json=lambda: {
        'usage': {'input_tokens': 100},
        'answers': {'next_action': {'choice': choice, 'confidence': confidence}}})


class ControllerTests(unittest.TestCase):
    def test_finite_actions_bind_profile_values(self):
        actions, reason = inspect_candidates(form(control()), {'email': 'grounded@example.test'})
        self.assertIsNone(reason)
        self.assertEqual(actions[0]['args'], {'ref': 'r0', 'value': 'grounded@example.test'})
        self.assertNotIn('submit', [a['tool'] for a in actions])

    def test_unknown_legal_required_question_fails_even_when_prefilled(self):
        for value in ('', 'No', 'Yes'):
            field = control(label='Will you require sponsorship now or in the future?')
            field['value'] = value
            self.assertEqual(inspect_candidates(form(field), {})[1],
                             'unsupported_question_or_prefilled_claim')

    def test_optional_populated_claim_and_checked_consent_block(self):
        for field in [dict(ref='r0',label='Recruiting communications',tag='input',type='checkbox',checked=True),
                      dict(ref='r0',label='Historical production work',tag='input',type='text',value='Yes')]:
            self.assertIsNotNone(inspect_candidates(form(field), {})[1])

    def test_missing_refs_custom_widgets_and_duplicate_refs_block(self):
        for state in [form(control(ref=None)), form(control(role_override=True)),
                      form(control(),control()), {'forms':0, 'controls':[]}]:
            if state.get('controls') and state['controls'][0].pop('role_override',False):
                state['controls'][0]['role']='combobox'
            self.assertIsNotNone(inspect_candidates(state, {'email':'a@b.test'})[1])

    def test_invalid_choices_abstentions_and_provider_errors_never_act(self):
        for choice, confidence in [('a123',1), ('unknown',1), ('email',.2), ('email',float('nan')),
                                   ('email', True), ('email',1.1), ('phone',1)]:
            planner, page = JevController('fake'), MagicMock()
            page.evaluate.return_value = form(control())
            with patch('applyd.apply.jev_controller.httpx.post', return_value=response(choice,confidence)):
                result, _ = planner.next(page, {'email':'a@b.test'}, 3, False)
            self.assertEqual(result.choices[0].message.tool_calls[0].function.name,'report_done')
        planner, page = JevController('fake'), MagicMock()
        page.evaluate.return_value = form(control())
        with patch('applyd.apply.jev_controller.httpx.post', side_effect=RuntimeError('SECRET')) as call:
            result, metrics = planner.next(page, {'email':'a@b.test'}, 3, False)
        self.assertNotIn('SECRET',str(metrics))
        self.assertEqual(call.call_count,1)

    def test_form_change_during_api_and_wrong_readback_block(self):
        planner, page = JevController('fake'), MagicMock()
        page.evaluate.side_effect = [form(control()), form(control(label='Sponsorship'))]
        with patch('applyd.apply.jev_controller.httpx.post', return_value=response()):
            _, metrics = planner.next(page, {'email':'a@b.test'}, 3, False)
        self.assertEqual(metrics['reason'],'form_changed_during_decision')
        planner.pending = {'label':'Email', 'value':'expected@example.test'}
        page.evaluate.side_effect = None
        page.evaluate.return_value = form(control())
        _, metrics = planner.next(page, {'email':'a@b.test'}, 4, True)
        self.assertEqual(metrics['reason'],'field_readback_mismatch')

    def test_existing_attachment_is_not_trusted(self):
        planner, page = JevController('fake'), MagicMock()
        page.evaluate.return_value = form(dict(ref='r0',label='Resume',tag='input',type='file',files=1))
        _, metrics = planner.next(page, {}, 3, True)
        self.assertEqual(metrics['reason'],'unverified_existing_attachment')

    def test_tool_errors_and_captcha_snapshots_stop_without_fallback(self):
        for name, result in [('fill','error: timeout'), ('snapshot','GATE DETECTED: CAPTCHA')]:
            planner, page = JevController('fake'), MagicMock()
            planner.observe(name,result)
            _, metrics = planner.next(page,{},3,False)
            self.assertEqual(metrics['reason'],'tool_failed_no_automatic_retry')
            page.evaluate.assert_not_called()

    def test_real_mode_rejected_before_browser_or_kimi(self):
        with patch.dict(os.environ, {'APPLYD_JEV_CONTROLLER':'true'}), \
             patch('applyd.apply.runner._make_client') as kimi, \
             patch('applyd.apply.runner.browser_page') as browser:
            result = run_apply(job_id='fixture',company='Example',title='Engineer',
                job_url='https://example.test',resume_pdf_path='unused.pdf',profile_md='{}',test_mode=False)
        self.assertEqual(result['status'],'review')
        kimi.assert_not_called()
        browser.assert_not_called()

    def test_exact_field_threshold_does_not_relax_submission_or_semantics(self):
        page=MagicMock();page.evaluate.return_value=form(control())
        for confidence,allowed in ((.89,False),(.90,True),(.93,True)):
            with patch('applyd.apply.jev_controller.httpx.post',return_value=response('email',confidence)):
                result,_=JevController('fake').next(page,{'email':'a@example.test'},3,False)
            self.assertEqual(result.choices[0].message.tool_calls[0].function.name=='fill',allowed)
        filled=control();filled['value']='a@example.test';page.evaluate.return_value=form(filled)
        with patch('applyd.apply.jev_controller.httpx.post',return_value=response('submit',.93)):
            result,_=JevController('fake').next(page,{'email':'a@example.test'},3,False)
        self.assertEqual(result.choices[0].message.tool_calls[0].function.name,'report_done')

    def test_changed_form_schema_blocks_live_before_api_or_submit(self):
        page=MagicMock();filled=control();filled['value']='a@example.test'
        page.evaluate.return_value=form(filled)
        with patch('applyd.apply.jev_controller.httpx.post') as api:
            _,metrics=JevController('fake',expected_schema='outdated').next(page,{'email':'a@example.test'},3,False)
        self.assertEqual(metrics['reason'],'form_schema_changed_since_validation')
        api.assert_not_called()

    def test_submission_confirmation_requires_later_model_observation(self):
        planner=JevController('fake');planner.observe('submit','ok: submission_confirmed via r20; form closed')
        page=MagicMock()
        with patch('applyd.apply.jev_controller.httpx.post',return_value=MagicMock(status_code=503)):
            result,_=planner.next(page,{},10,False)
        self.assertFalse(planner.confirmation_observed)
        args=json.loads(result.choices[0].message.tool_calls[0].function.arguments)
        self.assertEqual(args['status'],'review');self.assertIn('do not resubmit',args['note'])
        with patch('applyd.apply.jev_controller.httpx.post',return_value=MagicMock(status_code=200,
                json=lambda:{'answers':{'outcome':{'choice':'confirmed'}},'usage':{'input_tokens':100}})) as api:
            result,_=planner.next(page,{},11,False)
        self.assertTrue(planner.confirmation_observed)
        self.assertEqual(json.loads(result.choices[0].message.tool_calls[0].function.arguments)['status'],'applied')
        self.assertIn('submission_confirmed',json.dumps(api.call_args.kwargs['json']['state']))
        page.evaluate.assert_not_called()

    def test_manifest_is_job_input_url_and_time_bound(self):
        from applyd.apply.jev_validation import validate_manifest
        from datetime import datetime,timezone,timedelta
        record={'job_id':'job','test_mode':True,'application_url':'https://example.test/job',
            'validated_at':datetime.now(timezone.utc).isoformat(),'input_fingerprint':'current',
            'result':{'note':'test_mode:ready_for_review','jev_validated_schema':'a'*64}}
        with patch('applyd.apply.jev_validation.input_fingerprint',return_value='current'):
            self.assertEqual(validate_manifest(record,'job',{},'unused','https://example.test/job'),'a'*64)
            self.assertIsNone(validate_manifest(record,'other',{},'unused','https://example.test/job'))
            self.assertIsNone(validate_manifest(record,'job',{},'unused','https://example.test/other'))
            changed={**record,'input_fingerprint':'outdated'}
            self.assertIsNone(validate_manifest(changed,'job',{},'unused','https://example.test/job'))
            expired={**record,'validated_at':(datetime.now(timezone.utc)-timedelta(hours=2)).isoformat()}
            self.assertIsNone(validate_manifest(expired,'job',{},'unused','https://example.test/job'))

    def test_real_browser_full_loop_no_kimi_no_submission(self):
        from playwright.sync_api import sync_playwright, Error
        from applyd.apply.tools import dispatch as real_dispatch
        events = []
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(channel='chrome', headless=True)
            except Error as exc:
                if "doesn't exist" not in str(exc):
                    raise
                try:
                    browser = playwright.chromium.launch(headless=True)
                except Error as exc:
                    if "doesn't exist" not in str(exc):
                        raise
                    self.skipTest('Install Chrome or Playwright Chromium for the browser regression')
            page = browser.new_page()
            @contextmanager
            def fixture_browser(*args, **kwargs):
                yield page
            def fixture_dispatch(page, name, args, **kwargs):
                if name == 'navigate':
                    page.set_content('''<form onsubmit="window.submissions++;return false">
                    <label for="email">Email *</label><input id="email" type="email" required>
                    <button type="submit">Submit application</button></form>
                    <script>window.submissions=0</script>''')
                    return 'ok: fixture loaded'
                return real_dispatch(page,name,args,**kwargs)
            try:
                with patch.dict(os.environ, {'APPLYD_JEV_CONTROLLER':'true', 'TYPESAFE_API_KEY':'fake'}), \
                     patch('applyd.apply.runner._make_client') as kimi, \
                     patch('applyd.apply.runner.browser_page',fixture_browser), \
                     patch('applyd.apply.runner.dispatch',fixture_dispatch), \
                     patch('applyd.apply.runner.build_code_reader',return_value=None), \
                     patch('applyd.apply.jev_controller.httpx.post',side_effect=[response(),response('submit')]) as api:
                    result = run_apply(job_id='fixture',company='Example',title='Engineer',
                        job_url='https://example.test',resume_pdf_path='unused.pdf',
                        profile_md=json.dumps({'email':'grounded@example.test'}),test_mode=True,on_event=events.append)
                self.assertEqual(result['note'],'test_mode:ready_for_review',result)
                self.assertEqual(page.locator('#email').input_value(),'grounded@example.test')
                self.assertEqual(page.evaluate('window.submissions'),0)
                kimi.assert_not_called()
                self.assertEqual(api.call_count,2)
                self.assertNotIn('grounded@example.test',json.dumps(api.call_args.kwargs['json']))
                self.assertEqual({e['payload']['provider'] for e in events if e['event_type']=='model_turn'},
                                 {'jev_controller'})
            finally:
                browser.close()

    def test_ashby_container_excludes_only_attachment_ui_not_questions(self):
        from playwright.sync_api import sync_playwright
        from applyd.apply.tools import snapshot
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='chrome',headless=True)
            try:
                page = browser.new_page()
                page.set_content('''<div>
                  <div class="ashby-application-form-autofill-pane"><input type="file"><button>Upload file</button></div>
                  <label for="f">Email *</label><input id="f" type="email" required>
                  <div class="ashby-application-form-field-entry">
                    <div class="ashby-application-form-question-title">Resume</div>
                    <div class="ashby-application-form-input-file"><input type="file" required><button>Upload File</button></div>
                  </div>
                  <button class="ashby-application-form-submit-button" type="submit">Submit Application</button>
                </div>''')
                snapshot(page)
                state = page.evaluate(INSPECT_JS)
                self.assertEqual(state['forms'],1)
                self.assertEqual(len(state['controls']),3)
                actions,reason=inspect_candidates(state,{'email':'a@example.test'})
                self.assertIsNone(reason)
                self.assertEqual([a['tool'] for a in actions],['fill','upload_resume'])
                page.locator('label').evaluate("e=>e.textContent='Require sponsorship? *'")
                snapshot(page)
                self.assertEqual(inspect_candidates(page.evaluate(INSPECT_JS),{})[1],
                                 'unsupported_question_or_prefilled_claim')
            finally:
                browser.close()
