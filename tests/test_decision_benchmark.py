"""Local tests of the opt-in benchmark; never invoke model providers."""
import unittest

from scripts.benchmark_jev_kimi import fixtures, payload, score


class DecisionBenchmarkTests(unittest.TestCase):
    def test_extended_fixtures_are_well_formed(self):
        from scripts.decision_extended_fixtures import fixtures as extended
        cases = extended()
        self.assertEqual(len(cases), 36)
        self.assertEqual(len({case['id'] for case in cases}), 36)
        self.assertEqual(len({case['group'] for case in cases}), 6)
        for case in cases:
            self.assertIn(case['expected'], case['options'])
        self.assertNotIn('expected', payload(cases))

    def test_application_question_eval_has_100_hidden_labels(self):
        from scripts.application_question_eval_fixtures import fixtures as application
        cases = application()
        self.assertEqual(len(cases), 100)
        self.assertEqual(len({case['id'] for case in cases}), 100)
        self.assertEqual(len({case['group'] for case in cases}), 5)
        self.assertEqual(sum(case['severity'] == 'critical' for case in cases), 43)
        for case in cases:
            self.assertIn(case['expected'], case['options'])
        request = payload(cases)
        self.assertNotIn('expected', request)
        self.assertNotIn('severity', request)

    def test_observed_application_eval_has_150_cases(self):
        from scripts.observed_application_question_eval_fixtures import fixtures as observed
        cases = observed()
        self.assertEqual(len(cases), 150)
        self.assertEqual(len({case['id'] for case in cases}), 150)
        self.assertEqual(sum(case['id'].startswith('observed_') for case in cases), 50)
        self.assertTrue(all(len([c for c in cases if c['group'] == group]) <= 20
                            for group in {case['group'] for case in cases}))
        request = payload(cases)
        self.assertNotIn('expected', request)
        self.assertNotIn('observed_company', request)

    def test_fixture_labels_and_ids(self):
        cases = fixtures()
        self.assertEqual(len(cases), 24)
        self.assertEqual(len({case['id'] for case in cases}), len(cases))
        for case in cases:
            self.assertIn(case['expected'], case['options'])

    def test_expected_labels_are_not_sent_to_models(self):
        cases = fixtures()
        request = payload(cases)
        self.assertEqual(set(request), {'state', 'questions'})
        for case in cases:
            self.assertEqual(request['state'][case['id']], case['state'])
            question = request['questions'][case['id']]
            self.assertEqual(set(question), {'type', 'instructions', 'criteria'})
            self.assertIn(case['id'], question['instructions'])

    def test_missing_and_invalid_answers_are_incorrect(self):
        cases = fixtures()[:1]
        for answer in ({}, {'choice': 'made-up'}, {'choice': ['data']}, None):
            result = score(cases, {cases[0]['id']: answer})[0]
            self.assertFalse(result['valid'])
            self.assertFalse(result['correct'])

    def test_unknown_facts_are_not_false_answers(self):
        cases = [case for case in fixtures() if case['id'] == 'wrong_country']
        result = score(cases, {'wrong_country': {'choice': 'o1'}})[0]
        self.assertTrue(result['unsafe_answer'])
        self.assertFalse(result['correct'])
        result = score(cases, {'wrong_country': {'choice': 'review'}})[0]
        self.assertFalse(result['unsafe_answer'])
        self.assertTrue(result['correct'])


if __name__ == '__main__':
    unittest.main()
