"""Non-submitting browser regression for Ashby's pressed-state buttons."""
import unittest

from playwright.sync_api import sync_playwright

from applyd.apply.jev import DECISIONS_JS, JevPlanner, is_optional_answer_action, is_satisfied_answer_action, is_bounded_answer_action
from applyd.apply.runner import _test_mode_answer_audit, _visible_optional_gap
from applyd.apply.tools import snapshot
from applyd.apply.tools import fill, submit, _select_profile_guard


class AshbyChoiceStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = sync_playwright().start()
        try:
            cls.browser = cls.runtime.chromium.launch(channel='chrome', headless=True)
        except Exception as exc:
            cls.runtime.stop()
            raise unittest.SkipTest(f'Local Chrome unavailable: {type(exc).__name__}')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.runtime.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.set_content('''<div>
          <div class="ashby-application-form-question-title required">
            Are you eligible to work in the United States?</div>
          <div><button type="button" aria-pressed="false">Yes</button>
               <button type="button" aria-pressed="false">No</button></div>
        </div><script>
          for (const button of document.querySelectorAll('button')) {
            button.onclick=()=> {
              const pressed=button.getAttribute('aria-pressed')==='true';
              for (const other of button.parentElement.querySelectorAll('button'))
                other.setAttribute('aria-pressed', 'false');
              button.setAttribute('aria-pressed', pressed ? 'false' : 'true');
            };
          }
        </script>''')

    def tearDown(self):
        self.page.close()

    def test_requiredness_and_pressed_selection_survive_new_refs(self):
        rendered = snapshot(self.page)
        self.assertIn(' *]', rendered)
        group = self.page.evaluate(DECISIONS_JS)[0]
        self.assertTrue(group['required'])
        self.assertFalse(is_satisfied_answer_action(self.page, 'click', {'ref':group['options'][0]['ref']}, {group['question']}))
        self.assertFalse(is_optional_answer_action(self.page, 'click', {'ref':group['options'][0]['ref']}))
        planner = JevPlanner('not-a-real-key')
        profile = {'work_authorization': {'US': {'authorized':False}}}
        actions, _ = planner.propose(self.page, profile)
        self.assertEqual(actions[0][0], 'click')
        self.page.locator(f'[data-applyd-ref="{actions[0][1]["ref"]}"]').click()
        # An upload/snapshot can shift every DOM ref. A selected No must not
        # be offered again and clicked off just because its ref changed.
        self.page.evaluate("document.body.insertAdjacentHTML('afterbegin', '<button>New control</button>')")
        self.assertIn('selected', snapshot(self.page))
        self.assertEqual(self.page.evaluate(DECISIONS_JS), [])
        self.assertEqual(planner.propose(self.page, profile)[0], [])
        self.assertEqual(_test_mode_answer_audit(self.page), [{
            'question':'Are you eligible to work in the United States?', 'value':'No'}])
        refs = self.page.locator('[data-applyd-option]').evaluate_all("es=>es.map(e=>e.getAttribute('data-applyd-ref'))")
        self.assertTrue(is_satisfied_answer_action(self.page, 'click_many', {'refs':refs}, planner.decision_attempts))
        self.assertFalse(is_satisfied_answer_action(self.page, 'click_many', {'refs':refs}, {}))

    def test_standalone_required_privacy_acknowledgement_is_controller_owned(self):
        self.page.set_content('<label><input type="checkbox" required>By submitting, I acknowledge my personal data will be processed in accordance with Example’s Privacy Policy.</label>')
        snapshot(self.page)
        group = self.page.evaluate(DECISIONS_JS)[0]
        self.assertIn('Privacy Policy', group['question'])
        actions, metrics = JevPlanner('not-a-real-key').propose(self.page,
            {'application_policy':{'required_consents':True}})
        self.assertEqual(actions, [('click',{'ref':group['options'][0]['ref']})])
        self.assertEqual(metrics['predicate'], 'required_consent')

    def test_graduation_text_preserves_month_and_submit_blocks_inferred_day(self):
        self.page.set_content('''<label for="grad">Graduation Date or Anticipated Graduation Date</label>
            <input id="grad" type="text" required>
            <button type="button" onclick="window.sent=true">Submit Application</button>''')
        snapshot(self.page)
        ref = self.page.locator('#grad').get_attribute('data-applyd-ref')
        profile = {'expected_grad_date':'2027-04'}
        self.assertTrue(fill(self.page, ref, '04/01/2027', profile).startswith('ok:'))
        self.assertEqual(self.page.locator('#grad').input_value(), '2027-04')
        self.page.locator('#grad').fill('04/01/2027')
        button = self.page.locator('button').get_attribute('data-applyd-ref')
        self.assertIn('exact graduation day', submit(self.page, button, False, profile=profile))
        self.assertIsNone(self.page.evaluate('window.sent'))

    def test_day_specific_control_requires_actual_day(self):
        self.page.set_content('<label for="grad">Graduation Date</label><input id="grad" type="date" required>')
        snapshot(self.page)
        ref = self.page.locator('#grad').get_attribute('data-applyd-ref')
        self.assertIn('only a month', fill(self.page, ref, '2027-04-01', {'expected_grad_date':'2027-04'}))
        self.assertEqual(self.page.locator('#grad').input_value(), '')

    def test_residence_relative_legal_guard_uses_canada_for_us_role(self):
        profile = {'address_country':'Canada', 'work_authorization':{
            'US':{'authorized':False, 'requires_sponsorship':True},
            'CA':{'authorized':True, 'requires_sponsorship':False}}}
        for question, correct, wrong in [
            ('Are you legally authorized to work in the location you currently reside?', 'Yes', 'No'),
            ('Will you now, or in the future, require sponsorship for a work permit for the location you currently reside?', 'No', 'Yes'),
        ]:
            self.page.set_content(f'<label for="legal">{question}</label><select id="legal" required><option>Yes</option><option>No</option></select>')
            snapshot(self.page)
            ref = self.page.locator('#legal').get_attribute('data-applyd-ref')
            self.assertIsNone(_select_profile_guard(self.page, ref, correct, profile, '', ['United States']))
            self.assertIsNotNone(_select_profile_guard(self.page, ref, wrong, profile, '', ['United States']))
        question = 'Will you require sponsorship for the location you currently reside?'
        self.page.set_content(f'<label for="legal">{question}</label><input id="legal" required>')
        snapshot(self.page)
        ref = self.page.locator('#legal').get_attribute('data-applyd-ref')
        self.assertTrue(fill(self.page, ref, 'Yes', profile, '', ['United States']).startswith('ok:'))
        self.assertEqual(self.page.locator('#legal').input_value(), 'No')

    def test_workable_external_radiogroup_prompt_is_not_lost(self):
        self.page.set_content('''<div id="prompt">Are you authorized to work in Canada?</div>
          <fieldset role="radiogroup" aria-labelledby="prompt">
            <div role="radio" aria-checked="false" aria-required="true"><label>
              <input type="radio" tabindex="-1" aria-hidden="true" name="qa" value="true"><span>YES</span>
            </label></div>
            <div role="radio" aria-checked="false" aria-required="true"><label>
              <input type="radio" tabindex="-1" aria-hidden="true" name="qa" value="false"><span>NO</span>
            </label></div>
          </fieldset>''')
        rendered = snapshot(self.page)
        self.assertIn('Are you authorized to work in Canada? — YES', rendered)
        group = self.page.evaluate(DECISIONS_JS)[0]
        self.assertEqual(group['question'], 'Are you authorized to work in Canada?')
        self.assertEqual([o['label'] for o in group['options']], ['YES', 'NO'])
        self.assertTrue(group['required'])
        planner = JevPlanner('not-a-real-key')
        actions, _ = planner.propose(self.page, {'work_authorization':{'CA':{'authorized':True}}})
        self.assertEqual(actions, [('click', {'ref':group['options'][0]['ref']})])

    def test_text_authorization_does_not_use_sponsorship_as_authorization(self):
        question = 'Are you legally authorized to work in the country for this position without requiring employer-sponsored work authorization?'
        self.page.set_content(f'<label for="legal">{question}</label><input id="legal" required>')
        snapshot(self.page)
        ref = self.page.locator('#legal').get_attribute('data-applyd-ref')
        profile = {'work_authorization':{'US':{'authorized':False, 'requires_sponsorship':True}}}
        self.assertTrue(fill(self.page, ref, 'Yes', profile, '', ['South SF']).startswith('ok:'))
        self.assertEqual(self.page.locator('#legal').input_value(), 'No')

    def test_student_affiliation_is_not_used_as_employer(self):
        self.page.set_content('<label for="employer">Please list your current or most recent employer.</label><input id="employer" required>')
        snapshot(self.page)
        ref = self.page.locator('#employer').get_attribute('data-applyd-ref')
        profile = {'current_company':'Example University (student)'}
        self.assertIn('student affiliation is not an employer', fill(self.page, ref, profile['current_company'], profile))
        self.assertEqual(self.page.locator('#employer').input_value(), '')

    def test_optional_gap_requires_unique_visible_positive_optional_field(self):
        self.page.set_content('<label for="location">Which location are you applying for?</label><select id="location"><option>London</option></select>')
        snapshot(self.page)
        question = 'Which location are you applying for?'
        self.assertTrue(_visible_optional_gap(self.page, question))
        self.assertFalse(_visible_optional_gap(self.page, 'Unmatched question'))
        self.page.locator('#location').evaluate('e=>e.required=true')
        self.assertFalse(_visible_optional_gap(self.page, question))
        self.page.locator('#location').evaluate('e=>{e.required=false;e.removeAttribute("data-applyd-required")}')
        self.assertFalse(_visible_optional_gap(self.page, question))

    def test_autocomplete_cannot_bypass_closed_answer_ownership(self):
        self.page.set_content('<label for="age">Please select your age range.</label><input role="combobox" id="age">')
        snapshot(self.page)
        ref = self.page.locator('#age').get_attribute('data-applyd-ref')
        self.assertTrue(is_bounded_answer_action(self.page, 'fill_autocomplete', {'ref':ref}))
        self.assertTrue(is_optional_answer_action(self.page, 'fill_autocomplete', {'ref':ref}))
        self.page.locator('#age').evaluate('e=>e.required=true')
        self.assertFalse(is_optional_answer_action(self.page, 'fill_autocomplete', {'ref':ref}))

    def test_current_company_autofill_is_reconciled_and_submit_guarded(self):
        self.page.set_content('<label for="company">Current company</label><input id="company" value="Past employer"><button type="button" onclick="window.sent=true">Submit Application</button>')
        snapshot(self.page)
        profile = {'current_company':'University (student)'}
        button = self.page.locator('button').get_attribute('data-applyd-ref')
        self.assertIn('current company differs', submit(self.page, button, False, profile=profile))
        self.assertIsNone(self.page.evaluate('window.sent'))
        planner = JevPlanner('not-a-real-key')
        for _ in range(2):
            self.page.locator('#company').fill('Past employer')
            actions, _ = planner.propose(self.page, profile)
            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0][0], 'fill')
            self.assertEqual(actions[0][1]['value'], profile['current_company'])
            fill(self.page, **actions[0][1], profile=profile)
        self.assertEqual(planner.propose(self.page, profile)[0], [])

    def test_plain_language_multiselect_uses_only_known_languages(self):
        self.page.set_content('''<fieldset><legend>Language Skill(s) (Check all that apply) *</legend>
            <label><input type="checkbox">English (ENG)</label>
            <label><input type="checkbox">French (FRA)</label>
            <label><input type="checkbox">Mandarin Chinese</label></fieldset>''')
        snapshot(self.page)
        group = self.page.evaluate(DECISIONS_JS)[0]
        actions, metrics = JevPlanner('not-a-real-key').propose(self.page, {'spoken_languages':['English','French']})
        self.assertEqual(actions, [('click_many', {'refs':[o['ref'] for o in group['options'][:2]]})])
        self.assertEqual(metrics['predicate'], 'spoken_languages_multi')
        self.page.set_content('''<fieldset><legend>Language Skill(s) (Check all that apply) *</legend>
            <label><input type="checkbox">Native English</label>
            <label><input type="checkbox">Fluent French</label></fieldset>''')
        snapshot(self.page)
        actions, _ = JevPlanner('not-a-real-key').propose(self.page, {'spoken_languages':['English','French']})
        self.assertEqual(actions[0][0], 'report_done')

    def test_undergrad_discipline_multiselect_uses_current_bachelor_major(self):
        self.page.set_content('''<fieldset><legend>Undergrad Discipline(s) *</legend>
            <label><input type="checkbox">Computer Science</label>
            <label><input type="checkbox">Mathematics</label></fieldset>''')
        snapshot(self.page)
        group = self.page.evaluate(DECISIONS_JS)[0]
        actions, metrics = JevPlanner('not-a-real-key').propose(self.page,
            {'degree':'Bachelor of Computer Science','major':'Computer Science'})
        self.assertEqual(actions, [('click_many', {'refs':[group['options'][0]['ref']]})])
        self.assertEqual(metrics['predicate'], 'undergrad_discipline_multi')


if __name__ == '__main__':
    unittest.main()
