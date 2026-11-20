import unittest
from types import SimpleNamespace

import httpx

from applyd.discovery.ats.workable import fetch_description
from applyd.enrichment.fetcher import fetch_text, job_is_live


class WorkableDescriptionTests(unittest.TestCase):
    def test_posting_sections_are_normalized_by_adapter_and_cached(self):
        calls = []
        def respond(request):
            calls.append(str(request.url))
            return httpx.Response(200, json=dict(shortcode='ABC123', state='published',
                description='Grounded description. ' * 30,
                requirements='Bachelor degree or equivalent.', benefits='Benefits.'))
        cache = {('workable', 'example'): [SimpleNamespace(external_id='ABC123',
            url='https://apply.workable.com/example/j/ABC123/', description=None)]}
        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            text, tier, error = fetch_text('https://apply.workable.com/example/j/ABC123/apply', client, cache)
            self.assertEqual((tier, error), ('ats', None))
            self.assertIn('Bachelor degree or equivalent.', text)
            self.assertEqual(fetch_text('https://apply.workable.com/example/j/ABC123/', client, cache)[0], text)
        self.assertEqual(len(calls), 1)

    def test_wrong_or_closed_posting_never_returns_description(self):
        for payload in [dict(shortcode='WRONG', state='published'),
                        dict(shortcode='ABC123', state='closed')]:
            with self.subTest(payload=payload), httpx.Client(transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json=dict(payload, description='not usable')))) as client:
                self.assertIsNone(fetch_description('example', 'ABC123', client))

    def test_posting_missing_from_first_board_page_is_checked_directly(self):
        cache = {('workable', 'example'): [SimpleNamespace(external_id='OTHER',
            url='https://apply.workable.com/example/j/OTHER/', description=None)]}
        with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
                200, json=dict(shortcode='ABC123', state='published',
                               description='Grounded description. ' * 30)))) as client:
            url = 'https://apply.workable.com/example/j/ABC123/'
            self.assertTrue(job_is_live(url, client, cache))
            self.assertEqual(fetch_text(url, client, cache)[1], 'ats')

    def test_inconclusive_individual_posting_is_not_closed(self):
        cache = {('workable', 'example'): [SimpleNamespace(external_id='OTHER',
            url='https://apply.workable.com/example/j/OTHER/', description=None)]}
        with httpx.Client(transport=httpx.MockTransport(
                lambda request: httpx.Response(500))) as client:
            self.assertIsNone(job_is_live('https://apply.workable.com/example/j/ABC123/', client, cache))
