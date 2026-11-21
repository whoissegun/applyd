import unittest
from unittest.mock import patch

from applyd.apply.jev import authorized_actions, tool_response, JevPlanner


class JevHybridTests(unittest.TestCase):
    def test_decision_state_includes_source_resume_facts_not_contact_fields(self):
        from applyd.apply.jev import _decision_profile
        facts = {'projects':[{'name':'Example','bullets':[{'id':'source-1','text':'Built an AI application'}]}]}
        state = _decision_profile({'full_name':'Private Name','email':'private@example.test',
                                   'resume_facts':facts,'resume_employers':['Example Employer'],
                                   'academic_status':{'level':'bachelors','status':'in_progress'}})
        self.assertEqual(state['resume_facts'], facts)
        self.assertEqual(state['resume_employers'], ['Example Employer'])
        self.assertNotIn('email', state)
        self.assertNotIn('full_name', state)
        self.assertEqual(state['academic_status']['status'], 'in_progress')

    def test_required_radio_is_prioritized_over_optional_navigation(self):
        from unittest.mock import MagicMock
        optional = {'question':'Which location are you applying for?', 'kind':'select',
                    'ref':'r1','required':False,'options':[{'key':'o0','label':'Select...'}, {'key':'o1','label':'Remote'}]}
        required = {'question':'Are you authorized to work in the United States?',
                    'kind':'choice','required':True,'options':[
                        {'key':'yes','label':'Yes','ref':'r2'},{'key':'no','label':'No','ref':'r3'}]}
        page = MagicMock(); page.evaluate.side_effect = [[],[optional,required],[optional,required]]
        planner = JevPlanner('fake')
        with patch.object(planner, '_post') as post:
            actions, _ = planner.propose(page, {'work_authorization':{'US':{'authorized':False}}})
        self.assertEqual(actions, [('click', {'ref':'r3'})])
        post.assert_not_called()

    def test_optional_unknown_age_range_is_skipped_without_guessing_or_paid_request(self):
        from unittest.mock import MagicMock
        planner = JevPlanner('fake')
        page = MagicMock()
        page.evaluate.side_effect = [[], [{
            'question':'What is your age range?', 'kind':'choice', 'required':False,
            'options':[{'key':'a','label':'18-20','ref':'r1'},
                       {'key':'b','label':'21-29','ref':'r2'}]}]]
        with patch.object(planner, '_post') as post:
            actions, metrics = planner.propose(page, {'over_18':True})
        self.assertEqual(actions, [])
        self.assertEqual(metrics['reason'], 'optional_question_skipped')
        post.assert_not_called()

    def test_optional_bounded_skip_requires_positive_optional_detection(self):
        from unittest.mock import MagicMock
        from applyd.apply.jev import is_optional_answer_action
        page = MagicMock()
        page.locator.return_value.first.evaluate.return_value = True
        self.assertTrue(is_optional_answer_action(page, 'select_option', {'ref':'r5'}))
        page.locator.return_value.first.evaluate.return_value = False
        self.assertFalse(is_optional_answer_action(page, 'select_option', {'ref':'r5'}))
        self.assertFalse(is_optional_answer_action(page, 'pick_option', {'label':'Yes'}))

    def test_grounded_unselected_decision_can_retry_once_after_form_refresh(self):
        from unittest.mock import MagicMock
        planner = JevPlanner('fake', use_openrouter=True)
        group = {'question': 'Are you authorized to work in the United States?',
                 'kind': 'choice', 'required': True, 'options': [
                     {'key': 'yes', 'label': 'Yes', 'ref': 'r1'},
                     {'key': 'no', 'label': 'No', 'ref': 'r2'}]}
        page = MagicMock()
        profile = {'work_authorization': {'US': {'authorized': False}}}
        for index in range(2):
            if index:
                group = dict(group, options=[
                    {'key': 'yes', 'label': 'Yes', 'ref': 'r11'},
                    {'key': 'no', 'label': 'No', 'ref': 'r12'}])
            page.evaluate.side_effect = [[], [group], [group]]
            actions, _ = planner.propose(page, profile)
            self.assertEqual([('click', {'ref': 'r12' if index else 'r2'})], actions)
        page.evaluate.side_effect = [[], [group]]
        actions, _ = planner.propose(page, profile)
        self.assertEqual([], actions)

    def test_unmatched_optional_demographic_does_not_block_application(self):
        from unittest.mock import MagicMock
        planner = JevPlanner('fake', use_openrouter=True)
        group = {'question': 'Veteran status', 'kind': 'select', 'required': False,
                 'ref': 'r1', 'options': [
                     {'key': 'yes', 'label': 'I am a veteran'},
                     {'key': 'decline', 'label': 'Decline to self-identify'}]}
        page = MagicMock(); page.evaluate.side_effect = [[], [group]]
        actions, metrics = planner.propose(page, {'veteran_status': 'I am not a protected veteran'})
        self.assertEqual([], actions)
        self.assertEqual('optional_unmatched_decision_skipped', metrics['reason'])

    def test_equivalent_veteran_wording_is_bound(self):
        from applyd.apply.deterministic_questions import resolve_question
        answer = resolve_question('Veteran status', {'veteran_status': 'I am not a protected veteran'},
                                  {'yes': 'I am a veteran', 'no': 'I am not a veteran'})
        self.assertEqual('no', answer.choice)

    def test_observed_openai_availability_wording_uses_known_fact(self):
        from applyd.apply.runner import _profile_already_answers
        question='When can you start a new role?'
        self.assertTrue(_profile_already_answers(question,{'earliest_start_date':'immediately'}))
        self.assertFalse(_profile_already_answers(question,{}))
        self.assertFalse(_profile_already_answers('When did you start your last role?',
            {'earliest_start_date':'immediately'}))

    def test_value_is_bound_to_profile_not_model(self):
        fields = [dict(ref='r1', label='First name *', select=False)]
        answers = {'r1': dict(choice='first_name', confidence=0.99, value='Invented')}
        seen = set()
        self.assertEqual(authorized_actions(fields, answers, {'first_name': 'Example'}, seen),
                         [('fill', {'ref': 'r1', 'value': 'Example'})])
        self.assertEqual(authorized_actions(fields, answers, {'first_name': 'Example'}, seen), [])

    def test_unsafe_semantics_and_uncertainty_never_fill(self):
        for label, key, confidence, profile in [
            ('Require sponsorship now or later?', 'first_name', 1, {'first_name': 'No'}),
            ('Former first name', 'first_name', 1, {'first_name': 'Example'}),
            ('First name', 'last_name', 1, {'last_name': 'Example'}),
            ('First name', 'first_name', 0.94, {'first_name': 'Example'}),
            ('First name', 'first_name', 1, {}),
            ('Email', 'email', float('nan'), {'email': 'test@example.test'}),
        ]:
            self.assertEqual(authorized_actions([dict(ref='r1', label=label, select=False)],
                {'r1': dict(choice=key, confidence=confidence)}, profile, set()), [])

    def test_combobox_uses_guarded_selection_not_plain_fill(self):
        self.assertEqual(authorized_actions([dict(ref='r2', label='School', select=True)],
            {'r2': dict(choice='school', confidence=0.99)}, {'school': 'Example University'}, set()),
            [('select_option', {'ref': 'r2', 'value': 'Example University'})])

    def test_phone_country_combobox_uses_profile_country(self):
        from unittest.mock import MagicMock
        planner = JevPlanner('fake', use_openrouter=True)
        page = MagicMock()
        fields = [{
            'ref':'r4', 'label':'Phone', 'select':True,
            'inputType':'text', 'required':True,
        }]
        page.evaluate.side_effect = [fields, fields]
        actions, metrics = planner.propose(page, {
            'phone':'343-558-5872', 'address_country':'Canada',
        })
        self.assertEqual(actions, [('select_option', {'ref':'r4', 'value':'Canada'})])
        self.assertEqual(metrics['model'], 'deterministic_profile_policy')

    def test_grounded_country_and_degree_are_exact_mapped_fields(self):
        fields = [
            dict(ref='r1', label='Country', select=True),
            dict(ref='r2', label='Degree', select=True),
        ]
        answers = {
            'r1': dict(choice='address_country', confidence=.99),
            'r2': dict(choice='degree', confidence=.99),
        }
        self.assertEqual(authorized_actions(fields, answers, {
            'address_country':'Canada', 'degree':'Bachelor of Computer Science'}, set()), [
                ('select_option', {'ref':'r1', 'value':'Canada'}),
                ('select_option', {'ref':'r2', 'value':'Bachelor of Computer Science'}),
            ])

    def test_kimi_custom_checkbox_click_is_recognized_as_bounded(self):
        from unittest.mock import MagicMock
        from applyd.apply.jev import is_bounded_answer_action
        page = MagicMock()
        locator = page.locator.return_value.first
        locator.get_attribute.side_effect = lambda name: {
            'data-applyd-question':'', 'data-applyd-option':'I agree',
            'role':'checkbox', 'type':'button',
        }.get(name)
        self.assertTrue(is_bounded_answer_action(page, 'click', {'ref':'r4'}))

    def test_response_is_only_runner_owned_calls(self):
        response = tool_response([('fill', {'ref': 'r1', 'value': 'Example'})], 4)
        self.assertEqual(response.choices[0].message.tool_calls[0].function.name, 'fill')
        self.assertEqual(response.choices[0].message.model_dump()['role'], 'assistant')

    def test_missing_key_fails_closed(self):
        with self.assertRaises(ValueError):
            JevPlanner(None)

    def test_boolean_confidence_is_not_numeric_confidence(self):
        self.assertEqual(authorized_actions([dict(ref='r1',label='Email',select=False)],
            {'r1':dict(choice='email',confidence=True)}, {'email':'x@example.test'},set()), [])

    def test_stale_refs_and_invalid_usage_do_not_authorize_actions(self):
        from unittest.mock import MagicMock
        original=[dict(ref='r1',label='Email contact',select=False)]
        for tokens, fresh, reason in [
            (100,[dict(ref='r1',label='Sponsorship',select=False)],'form_changed_during_decision'),
            (-1,original,'invalid_provider_usage'),
            (True,original,'invalid_provider_usage'),
        ]:
            planner=JevPlanner('fake',max_requests=15); page=MagicMock()
            page.evaluate.side_effect=[original,fresh]
            response=MagicMock(status_code=200,json=lambda:{'usage':{'input_tokens':tokens},
                'model':'jev','answers':{'r1':{'choice':'email','confidence':.99}}})
            with patch('applyd.apply.jev.httpx.post',return_value=response):
                actions,info=planner.propose(page,{'email':'x@example.test'})
            self.assertEqual(actions,[]);self.assertEqual(info['reason'],reason)
            self.assertEqual(planner.seen,set())

    def test_assisted_planner_can_reenter_on_new_page_fields(self):
        from unittest.mock import MagicMock
        planner=JevPlanner('fake',max_requests=15);page=MagicMock()
        for label,key,value in [('Email','email','x@example.test'),('City','address_city','Ottawa')]:
            page.evaluate.return_value=[dict(ref='r1',label=label,select=False)]
            response=MagicMock(status_code=200,json=lambda:{'usage':{'input_tokens':100},
                'model':'jev','answers':{'r1':{'choice':key,'confidence':.99}}})
            with patch('applyd.apply.jev.httpx.post',return_value=response):
                actions,_=planner.propose(page,{key:value})
            self.assertEqual(actions,[('fill',{'ref':'r1','value':value})])
        self.assertEqual(planner.requests,0)

    def test_pure_controller_remains_separate_from_default_assisted_path(self):
        from applyd.apply.runner import run_apply
        with patch.dict('os.environ',{'APPLYD_JEV_CONTROLLER':'true'}), \
             patch('applyd.apply.runner.browser_page') as browser:
            result = run_apply(job_id='fictional',company='Example',title='Engineer',
                job_url='https://example.test',resume_pdf_path='none',profile_md='{}',test_mode=False)
        self.assertEqual(result['status'],'review')
        browser.assert_not_called()

    def test_runner_uses_jev_then_kimi_without_false_confirmation(self):
        from contextlib import contextmanager
        from unittest.mock import MagicMock
        from applyd.apply.runner import run_apply
        page=MagicMock()
        page.evaluate.return_value=[dict(ref='r1',label='Email',select=False)]
        @contextmanager
        def browser(*args,**kwargs):
            yield page
        client=MagicMock()
        client.chat.completions.create.side_effect=[
            tool_response([('preflight',{'missing_fields':[],'answerable_required_labels':['Email']})],2),
            tool_response([('report_done',{'status':'applied','note':'Unsupported success claim'})],4),
            tool_response([('report_done',{'status':'review','note':'No submission confirmation'})],5),
        ]
        response=MagicMock(status_code=200,json=lambda:{'usage':{'input_tokens':100},
            'model':'jev','answers':{'r1':{'choice':'email','confidence':.99}}})
        events=[]
        with patch.dict('os.environ',{'APPLYD_JEV_CONTROLLER':'false','APPLYD_JEV_ASSISTED':'true','TYPESAFE_API_KEY':'fake'}), \
             patch('applyd.apply.runner.browser_page',browser), \
             patch('applyd.apply.runner._make_client',return_value=client), \
             patch('applyd.apply.runner.dispatch',return_value='ok: inspected'), \
             patch('applyd.apply.runner.build_code_reader',return_value=None), \
             patch('applyd.apply.jev.httpx.post',return_value=response):
            result=run_apply(job_id='fictional',company='Example',title='Engineer',
                job_url='https://example.test',resume_pdf_path='none',
                profile_md='{"email":"x@example.test"}',test_mode=True,on_event=events.append)
        providers=[e['payload']['provider'] for e in events if e['event_type']=='model_turn']
        self.assertEqual(providers[0], 'kimi')
        self.assertIn('jev_assisted', providers)
        self.assertNotEqual(result['status'],'applied')
        self.assertNotIn('runner exception',result['note'])
        self.assertEqual(result['jev_estimated_cost_usd'],0)

    def test_provider_error_is_sanitized_and_disables_retries(self):
        from unittest.mock import MagicMock
        planner = JevPlanner('not-a-real-secret')
        page = MagicMock()
        page.evaluate.return_value = [dict(ref='r1', label='Email', select=False)]
        with patch('applyd.apply.jev.httpx.post', side_effect=RuntimeError('secret')) as call:
            actions, info = planner.propose(page, {})
            self.assertEqual(actions, [])
            self.assertNotIn('secret', str(info))
            planner.propose(page, {})
            self.assertEqual(call.call_count, 1)

    def test_mixed_us_canada_legal_question_routes_to_review(self):
        from unittest.mock import MagicMock
        planner = JevPlanner('fake', use_openrouter=True)
        page = MagicMock()
        page.evaluate.side_effect = [[], [{
            'question': 'Are you legally authorized to work in the U.S. or Canada?',
            'kind': 'choice', 'required': True,
            'options': [
                {'key':'o0','label':'Yes','ref':'r1'},
                {'key':'o1','label':'No','ref':'r2'},
            ],
        }]]
        actions, metrics = planner.propose(page, {'work_authorization': {
            'US': {'authorized': False}, 'CA': {'authorized': True},
        }})
        self.assertEqual(actions[0][0], 'report_done')
        self.assertEqual(metrics['reason'], 'deterministic_review')

    def test_jev_selects_visible_radio_option_from_profile(self):
        from unittest.mock import MagicMock
        planner = JevPlanner('fake', use_openrouter=True)
        page = MagicMock()
        groups = [{'question':'Are you authorized to work in the United States?',
            'kind':'choice','required':True,'options':[
                {'key':'o0','label':'Yes','ref':'r1'},
                {'key':'o1','label':'No','ref':'r2'}]}]
        page.evaluate.side_effect = [[], groups, groups]
        response = MagicMock(status_code=200, json=lambda:{
            'model':'typesafe/jev-1.13','usage':{'input_tokens':100,'cost':.00001},
            'answers':{'answer':{'choice':'o1','confidence':.99}}})
        with patch('applyd.apply.jev.httpx.post', return_value=response) as post:
            actions, metrics = planner.propose(page, {'work_authorization': {
                'US': {'authorized': False, 'requires_sponsorship': True}}})
        self.assertEqual(actions, [('click', {'ref':'r2'})])
        self.assertEqual(metrics['model'], 'deterministic_profile_policy')
        post.assert_not_called()

    def test_deterministic_legal_text_field_bypasses_models(self):
        from unittest.mock import MagicMock
        planner = JevPlanner('fake', use_openrouter=True)
        page = MagicMock()
        fields = [{
            'ref': 'r12',
            'label': 'Are you authorized to work in the U.S.?',
            'select': False,
            'inputType': 'text',
            'required': True,
        }]
        page.evaluate.side_effect = [fields, fields]
        with patch('applyd.apply.jev.httpx.post') as post:
            actions, metrics = planner.propose(page, {'work_authorization': {
                'US': {'authorized': False, 'requires_sponsorship': True},
            }})
        self.assertEqual(actions, [('fill', {'ref': 'r12', 'value': 'No'})])
        self.assertEqual(metrics['model'], 'deterministic_profile_policy')
        post.assert_not_called()

    def test_required_checkbox_group_can_use_deterministic_residence(self):
        from unittest.mock import MagicMock
        planner = JevPlanner('fake', use_openrouter=True)
        page = MagicMock()
        group = {'question':'What is your country of residence?',
            'kind':'checkbox','required':True,'options':[
                {'key':'o0','label':'Other','ref':'r13'},
                {'key':'o1','label':'Canada','ref':'r14'},
                {'key':'o2','label':'U.S.','ref':'r15'}]}
        page.evaluate.side_effect = [[], [group], [group]]
        with patch('applyd.apply.jev.httpx.post') as post:
            actions, metrics = planner.propose(page, {'address_country':'Canada'})
        self.assertEqual(actions, [('click', {'ref':'r14'})])
        self.assertEqual(metrics['model'], 'deterministic_profile_policy')
        post.assert_not_called()

    def test_prerequisite_fields_run_before_dependent_relative_review(self):
        from unittest.mock import MagicMock
        planner = JevPlanner('fake', use_openrouter=True)
        page = MagicMock()
        fields = [
            {'ref':'r22', 'label':'Please select the country where you currently reside. *',
             'select':True, 'inputType':'text', 'required':True},
            {'ref':'r54', 'label':'Are you authorized to work in the location(s) you selected in your previous response?*',
             'select':True, 'inputType':'text', 'required':True},
        ]
        page.evaluate.side_effect = [fields, fields]
        actions, metrics = planner.propose(page, {
            'address_country':'Canada',
            'work_authorization': {'CA': {'authorized':True}},
        })
        self.assertEqual(actions, [('select_option', {'ref':'r22', 'value':'Canada'})])
        self.assertEqual(metrics['model'], 'deterministic_profile_policy')

    def test_selected_location_checkbox_unblocks_relative_authorization(self):
        from unittest.mock import MagicMock
        planner = JevPlanner('fake', use_openrouter=True)
        page = MagicMock()
        fields = [{
            'ref':'r54',
            'label':'Are you authorized to work in the location(s) you selected in your previous response?*',
            'select':True, 'inputType':'text', 'required':True,
        }]
        page.evaluate.side_effect = [fields, ['Canada'], fields]
        actions, metrics = planner.propose(page, {
            'work_authorization': {'CA': {'authorized':True}},
        })
        self.assertEqual(actions, [('select_option', {'ref':'r54', 'value':'Yes'})])
        self.assertEqual(metrics['model'], 'deterministic_profile_policy')

    def test_dependent_relative_review_waits_for_location_checkbox_group(self):
        from unittest.mock import MagicMock
        planner = JevPlanner('fake', use_openrouter=True)
        page = MagicMock()
        fields = [{
            'ref':'r54',
            'label':'Are you authorized to work in the location(s) you selected in your previous response?*',
            'select':True, 'inputType':'text', 'required':True,
        }]
        group = {'question':'What is your country of residence?',
            'kind':'checkbox','required':True,'options':[
                {'key':'o0','label':'Canada','ref':'r27'},
                {'key':'o1','label':'US','ref':'r53'}]}
        page.evaluate.side_effect = [fields, [], [group], [group]]
        actions, metrics = planner.propose(page, {
            'address_country':'Canada',
            'work_authorization': {'CA': {'authorized':True}},
        })
        self.assertEqual(actions, [('click', {'ref':'r27'})])
        self.assertEqual(metrics['model'], 'deterministic_profile_policy')

    def test_jev_never_uses_a_choice_after_form_changes(self):
        from unittest.mock import MagicMock
        planner = JevPlanner('fake', use_openrouter=True)
        page = MagicMock()
        group = {'question':'Are you authorized to work in the United States?',
            'kind':'choice','required':True,'options':[
                {'key':'o0','label':'Yes','ref':'r1'},
                {'key':'o1','label':'No','ref':'r2'}]}
        changed = dict(group, options=[
            {'key':'o0','label':'Yes','ref':'r8'},
            {'key':'o1','label':'No','ref':'r9'}])
        page.evaluate.side_effect = [[], [group], [changed]]
        with patch('applyd.apply.jev.httpx.post') as post:
            actions, metrics = planner.propose(page, {'work_authorization': {
                'US': {'authorized': False}}})
        self.assertEqual(actions[0][0], 'report_done')
        self.assertEqual(metrics['reason'], 'form_changed_during_decision')
        post.assert_not_called()

    def test_closed_decision_threshold_is_point_nine_five(self):
        from unittest.mock import MagicMock
        group = {'question':'Do you enjoy pair programming?',
            'kind':'choice','required':True,'options':[
                {'key':'o0','label':'Yes','ref':'r1'},
                {'key':'o1','label':'No','ref':'r2'}]}
        for confidence, expected_tool in ((.949, 'report_done'), (.95, 'click')):
            planner = JevPlanner('fake', use_openrouter=True)
            page = MagicMock(); page.evaluate.side_effect = [[], [group], [group]]
            response = MagicMock(status_code=200, json=lambda confidence=confidence:{
                'model':'typesafe/jev-1.13','usage':{'input_tokens':100,'cost':.00001},
                'answers':{'answer':{'choice':'o0','confidence':confidence}}})
            with patch('applyd.apply.jev.httpx.post', return_value=response):
                actions, metrics = planner.propose(page, {'work_authorization': {
                    'CA': {'authorized': True}}})
            self.assertEqual(actions[0][0], expected_tool)
            self.assertEqual(metrics['threshold'], .95)

    def test_test_mode_answer_audit_keeps_answers_but_excludes_pii(self):
        from unittest.mock import MagicMock
        from applyd.apply.runner import _test_mode_answer_audit
        page = MagicMock()
        page.evaluate.return_value = [
            {'question':'Email address', 'value':'person@example.test'},
            {'question':'Name', 'value':'Private Person'},
            {'question':'Are you authorized to work in Canada?', 'value':'Yes'},
            {'question':'Why do you want this role?', 'value':'I enjoy distributed systems.'},
        ]
        self.assertEqual(_test_mode_answer_audit(page), [
            {'question':'Are you authorized to work in Canada?', 'value':'Yes'},
            {'question':'Why do you want this role?', 'value':'I enjoy distributed systems.'},
        ])
