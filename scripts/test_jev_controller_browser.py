"""Ten fictional local browser forms. --run uses paid Jev, never an employer.

Needs local Chrome. Reads TYPESAFE_API_KEY from env/.env, never the candidate
profile, resume, database, mail, or persistent application browser profile.
"""
import argparse
import json
from contextlib import contextmanager
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from applyd.apply.runner import run_apply
from applyd.apply.tools import dispatch
from applyd.config import load_env


FIXTURES = [
    ('email', '<label for="f">Email *</label><input id="f" type="email" required>', {'email':'fictional@example.test'}, True),
    ('name', '<label for="f">Full name *</label><input id="f" required>', {'full_name':'Fictional Candidate'}, True),
    ('phone', '<label for="f">Phone *</label><input id="f" type="tel" required>', {'phone':'+1 555 010 0100'}, True),
    ('city', '<label for="f">City *</label><input id="f" required>', {'address_city':'Ottawa'}, True),
    ('native_select', '<label for="f">Province *</label><select id="f" required><option value="">Choose</option><option value="ON">Ontario</option></select>', {'address_region':'Ontario'}, True),
    ('sponsorship', '<label for="f">Will you require sponsorship now or in the future? *</label><input id="f" required value="No">', {}, False),
    ('consent', '<label for="f">Recruiting communications</label><input id="f" type="checkbox" checked>', {}, False),
    ('nda', '<label for="f">Accept confidentiality agreement *</label><input id="f" type="checkbox" required>', {}, False),
    ('history', '<label for="f">Have you built production ML systems? *</label><input id="f" required>', {}, False),
    ('custom_widget', '<label for="f">School *</label><input id="f" role="combobox" required>', {'school':'Example University'}, False),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true',help='Make paid TypeSafe calls on fictional forms')
    args = parser.parse_args()
    if not args.run:
        print(json.dumps({'fixtures':len(FIXTURES),'network_calls':0,'instruction':'Add --run for paid Jev/local Chrome test.'}))
        return 0
    load_env()
    results = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='chrome',headless=True)
        try:
            for case, html, profile, expected_ready in FIXTURES:
                page = browser.new_page()
                events = []
                @contextmanager
                def local_browser(*args, **kwargs):
                    yield page
                def local_dispatch(page, name, args, **kwargs):
                    if name == 'navigate':
                        page.set_content('<form onsubmit="window.submissions++;return false">'+html+
                            '<button type="submit">Submit application</button></form><script>window.submissions=0</script>')
                        return 'ok: local fictional fixture loaded'
                    return dispatch(page,name,args,**kwargs)
                with patch.dict('os.environ',{'APPLYD_JEV_CONTROLLER':'true'}), \
                     patch('applyd.apply.runner.browser_page',local_browser), \
                     patch('applyd.apply.runner.dispatch',local_dispatch), \
                     patch('applyd.apply.runner.build_code_reader',return_value=None), \
                     patch('applyd.apply.runner._make_client',side_effect=AssertionError('Kimi must not be constructed')):
                    result = run_apply(job_id='fixture:'+case,company='Fictional',title='Local fixture',
                        job_url='https://example.invalid',resume_pdf_path='unused.pdf',
                        profile_md=json.dumps(profile),test_mode=True,max_cost_usd=.01,on_event=events.append)
                ready = result['note']=='test_mode:ready_for_review'
                submitted = page.evaluate('window.submissions')
                results.append({'case':case,'expected_ready':expected_ready,'ready':ready,
                    'pass':ready==expected_ready and submitted==0,'submissions':submitted,
                    'note':result['note'],'turns':result['turn_count'],
                    'jev_estimated_cost_usd':result['jev_estimated_cost_usd'],
                    'last_decision':next((e['payload'] for e in reversed(events)
                        if e['event_type']=='jev_controller_decision'),None)})
                page.close()
        finally:
            browser.close()
    print(json.dumps({'fixtures':results,'passed':sum(r['pass'] for r in results),
                      'count':len(results)},indent=2))
    return 0 if all(r['pass'] for r in results) else 1


if __name__=='__main__':
    raise SystemExit(main())
