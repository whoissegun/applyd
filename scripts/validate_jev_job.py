"""Non-submitting validation of one fresh, eligible, already-tailored job.

Does not create an application attempt or change database state. No fallback
model, no submission flag, no candidate values printed. Uses configured local
Chrome. TYPESAFE_API_KEY is required in the process environment or .env.
"""
import argparse
import contextlib
import io
import json
import os
from datetime import datetime,timezone
from pathlib import Path

from applyd.config import load_env
from applyd.local_store import get_local_store
from applyd.deduplication import canonical_ats_identity
from applyd.commands.apply_batch import _matches_prior_role
from applyd.discovery.routing import preferred_apply_url
from applyd.apply.runner import run_apply
from applyd.apply.jev_validation import application_url,input_fingerprint


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('job_id')
    parser.add_argument('--report',help='Write a private validation manifest for an explicit later live canary')
    args=parser.parse_args()
    load_env()
    store=get_local_store()
    job=store.get(args.job_id)
    if job is None:
        parser.error('Unknown job')
    prior=list(store.iter_prior_application_jobs())
    if store.get_apply_attempts(job.id) or _matches_prior_role(job.company,job.title,
            [(j.company.casefold(),j.title) for j in prior]) or any(
            canonical_ats_identity(job) is not None and canonical_ats_identity(job)==canonical_ats_identity(j)
            for j in prior):
        parser.error('Previously attempted job or alias: inspect manually instead')
    if (store.get_evaluation(job.id) or {}).get('decision')!='eligible':
        parser.error('Job is not currently eligible')
    application=store.get_application_by_job(job.id) or {}
    pdf=Path(application.get('pdf_path') or '')
    tex=Path(application.get('latex_path') or '')
    if not pdf.is_file() or not tex.is_file():
        parser.error('Tailor this job before validation')
    profile,_=store.get_profile()
    url=preferred_apply_url(job.id,job.url,company=job.company)
    url=application_url(url)
    os.environ['APPLYD_JEV_CONTROLLER']='true'
    events=[]
    with contextlib.redirect_stderr(io.StringIO()):
        result=run_apply(job_id=job.id,company=job.company,title=job.title,
            job_url=url,resume_pdf_path=str(pdf.resolve()),resume_tex=tex.read_text(),
            profile_md=json.dumps(profile),job_locations=list(job.locations),
            test_mode=True,browser_provider='local',max_cost_usd=.02,on_event=events.append)
    report={'job_id':job.id,'company':job.company,'title':job.title,
        'validated_at':datetime.now(timezone.utc).isoformat(),'application_url':url,
        'input_fingerprint':input_fingerprint(profile,pdf),
        'test_mode':True,'result':result,
        'controller_decisions':[e['payload'] for e in events if e['event_type']=='jev_controller_decision'],
        'providers':sorted({e['payload']['provider'] for e in events if e['event_type']=='model_turn'})}
    if args.report:
        destination=Path(args.report)
        destination.parent.mkdir(parents=True,exist_ok=True)
        with destination.open('x') as output:
            os.chmod(destination,0o600)
            json.dump(report,output,indent=2)
    print(json.dumps(report,indent=2))
    return 0 if result['note']=='test_mode:ready_for_review' else 1


if __name__=='__main__':
    raise SystemExit(main())
