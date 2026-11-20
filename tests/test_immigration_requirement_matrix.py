"""100 requirement-predicate cases; no browser, provider, or submissions."""
import unittest

from applyd.apply.deterministic_questions import resolve_question, resolve_text_question


class ImmigrationRequirementMatrixTests(unittest.TestCase):
    def test_100_cases_preserve_requirement_vs_possession(self):
        countries = {'US': 'the United States', 'CA': 'Canada',
                     'UK': 'the United Kingdom', 'EU': 'the European Union'}
        wordings = [
            'Do you require any visas or work authorizations to work in {}?',
            'Do you need a work permit to work in {}?',
            'Will you require a visa to work in {}?',
            'Do you require authorization to work in {}?',
            'Will you need work authorisation to work in {}?',
        ]
        records = [dict(authorized=False, requires_sponsorship=True),
                   dict(authorized=False, requires_sponsorship=False),
                   dict(authorized=True, requires_sponsorship=False),
                   dict(authorized=True, requires_sponsorship=True), {}]
        count = 0
        for region, country in countries.items():
            for wording in wordings:
                for record in records:
                    count += 1
                    question = wording.format(country)
                    profile = {'work_authorization': {region: record}}
                    expected = 'yes' if record.get('authorized') is False else 'review'
                    with self.subTest(question=question, record=record):
                        answer = resolve_question(question, profile,
                            {'yes': 'Yes', 'no': 'No', 'review': 'Review'})
                        self.assertEqual(answer.choice, expected)
                        text = resolve_text_question(question, profile, required=True)
                        self.assertIsNotNone(text)
                        self.assertEqual(text.value, 'Yes' if expected == 'yes' else None)
        self.assertEqual(count, 100)

    def test_explicit_california_does_not_use_canadian_residence_or_role(self):
        profile = {'work_authorization': {
            'US': {'authorized': False, 'requires_sponsorship': True},
            'CA': {'authorized': True, 'requires_sponsorship': False}}}
        answer = resolve_question(
            'Do you require any visas or work authorizations to work in California?',
            profile, {'yes': 'Yes', 'no': 'No'}, job_locations=['Canada'])
        self.assertEqual(answer.choice, 'yes')
