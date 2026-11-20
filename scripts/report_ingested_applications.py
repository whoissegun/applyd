"""Read-only SQLite audit of an ingestion manifest's application outcomes."""
from __future__ import annotations

import argparse
import ast
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from applyd.apply.runner import _TEST_AUDIT_SENSITIVE_LABEL
from applyd.commands.apply_batch import _matches_prior_role
from applyd.deduplication import canonical_ats_identity
from applyd.local_store import get_local_store


def review_triage(job):
    """Separate known stop mechanisms; do not pretend a code fix is verified."""
    reason = str(job.get('reason') or '')
    if 'submission_unconfirmed' in reason:
        return 'submission_uncertain', 'Do not retry automatically; verify receipt/outcome first.'
    if 'captcha' in reason.lower():
        return 'manual_captcha', 'CAPTCHA needs a manual path; not a missing profile answer.'
    questions = ' '.join(str(detail.get('question') or detail.get('label') or '')
                         for detail in job.get('review_details', []))
    errors = ' '.join(str(detail.get('error') or detail.get('issue') or '')
                      for detail in job.get('review_details', []))
    context = (reason + ' ' + questions + ' ' + errors).casefold()
    if 'exact graduation day' in context or 'day-specific graduation date' in context:
        return 'profile_date_precision', 'Submission was prevented. Provide an expected graduation day or explicitly approve an estimated day; do not accept the widget defaulting a month to its first day.'
    if any(term in context for term in (
        'native language', 'mandarin chinese', 'offer deadlines', 'criminal offence',
        'criminal offense', 'convicted', 'afrotech', 'access code', 'rank within your cohort',
        'your ppa', 'uk driving license', 'current remuneration package', 'high school diploma',
        'your experience with claude code',
        'supplier market research firm', 'writing sample',
    )):
        return 'user_fact_or_access', 'Answer the exact recorded question from your own facts; do not infer native fluency, credentials, legal history, deadlines, access codes, or current compensation.'
    if 'right to work status' in context:
        return 'legal_status_category', 'Authorization and sponsorship booleans do not establish a specific visa/status category. Confirm the actual current status before choosing a named category or Other.'
    if 'repeated_tool_failure' in reason and 'validation errors remain' in context:
        return 'code_or_mapping', 'The form rejected the recorded input despite repeated validation attempts. Inspect the exact question, widget state, and validation error before a safe retry; this is not a confirmed submission.'
    if 'completed information for all previous employers' in context:
        return 'attestation_review', 'Verify the requested employment-history coverage and what the form actually contains before attesting; required consent alone does not prove complete history.'
    if 'jev_required' in reason or 'submit_forbidden' in reason:
        return 'code_or_mapping', 'The bounded-answer controller blocked a Kimi selection. Inspect/fix the control or deterministic mapping, then test before a safe retry.'
    if 'safety_pause' in reason:
        return 'paused_before_submit', 'No submit was called; this job was paused during the earlier run.'
    if job.get('company') in {'Axon', 'Blissway', 'Fasanara'} and 'missing_info' in reason:
        return 'user_fact_or_access', 'Review the exact required questions below; do not invent academic ranks, legal history, prior-application history, or access credentials.'
    if 'missing_info' in reason and (
            job.get('company') in {'Node.Digital', 'Provectus', 'Porsche'}
            or (job.get('company') == 'CWILL' and 'fields=YES' in reason)):
        return 'profile_or_form_mapping', 'The hold includes missing question context, a conditional field, or an answer already in the profile. Inspect/fix the form mapping before asking the user.'
    if 'missing_info' in reason and any(term in reason.lower() for term in (
        'github', 'notice period', 'desired pay', 'labels not visible', 'country or profile fact',
    )):
        return 'profile_or_form_mapping', 'Check the existing profile/form context before asking the user; these may be false missing-information holds.'
    if 'missing_info' in reason:
        return 'question_review', 'Inspect the question and its context; determine whether it needs an actual fact, a user choice, or a mapping fix.'
    if 'deterministic' in reason or 'jev_decision' in reason:
        return 'decision_or_mapping', 'A deterministic mapping, jurisdiction, multi-select, or confidence check could not be resolved safely. Inspect the recorded question; do not force a choice.'
    return 'runtime_or_other', 'Inspect the recorded error before deciding whether a retry is safe.'


def answers_from_trace(events):
    """Reconstruct successful mutations, not merely model-proposed answers."""
    refs, pending, pending_questions, answers, issues = {}, {}, {}, [], []
    for event in events:
        payload = event.get('payload') or {}
        name = event.get('name')
        if event['event_type'] == 'answer_audit':
            for row in payload.get('answers', []):
                question, value = str(row.get('question', '')), str(row.get('value', ''))
                if question and value and not _TEST_AUDIT_SENSITIVE_LABEL.search(question):
                    answers.append({'question':question, 'answer':value,
                                    'evidence':'pre_submit_dom_audit', 'turn':event.get('turn')})
        if event['event_type'] == 'tool_call':
            pending[name] = payload.get('args') or {}
            pending_questions[name] = refs.get(pending[name].get('ref'), '')
        if event['event_type'] == 'jev_decision':
            if payload.get('question') and (payload.get('choice') == 'review'
                    or 'review' in str(payload.get('reason', ''))
                    or payload.get('confidence', 1) < payload.get('threshold', 0)):
                issues.append(payload)
        if event['event_type'] in {'safety_incident', 'profile_gap', 'answer_quality_flag'}:
            issues.append(payload)
        if event['event_type'] != 'tool_result':
            continue
        result = str(payload.get('result') or '')
        if name == 'snapshot' or re.search(r'(?:^|\n)r\d+: \[', result):
            refs = {}
            for line in result.splitlines():
                match = re.match(r'(r\d+): \[[^\]]+\] (.+)', line)
                if not match:
                    continue
                raw = match[2].split(' value=', 1)[0]
                try:
                    refs[match[1]] = ast.literal_eval(raw)
                except (SyntaxError, ValueError):
                    refs[match[1]] = raw
        args = pending.get(name) or {}
        question = pending_questions.get(name) or refs.get(args.get('ref'), '')
        if result.startswith('error:'):
            issues.append({'tool': name, 'question': question,
                           'error': result})
        if not result.startswith('ok:'):
            continue
        value = args.get('value')
        if name == 'select_option':
            selected = re.search(r"selected '(.*)'", result)
            value = selected[1] if selected else value
        elif name == 'click' and ' — ' in question:
            question, value = question.rsplit(' — ', 1)
        elif name not in {'fill', 'fill_autocomplete'}:
            continue
        if question and value and not _TEST_AUDIT_SENSITIVE_LABEL.search(question):
            answers.append({'question': question, 'answer': str(value),
                            'evidence': 'successful_tool_result', 'turn': event.get('turn')})
    return answers, issues


def build_report(manifest):
    store = get_local_store()
    prior = list(store.iter_prior_application_jobs())
    roles = [(j.company.casefold(), j.title) for j in prior]
    identities = {canonical_ats_identity(j) for j in prior}
    jobs = []
    for row in manifest['jobs']:
        jid = row['job_id']
        job = store.get(jid)
        attempts = store.get_apply_attempts(jid)
        application = store.get_application_by_job(jid) or {}
        evaluation = store.get_evaluation(jid) or {}
        liveness = store.get_liveness(jid) or {}
        outcome = {'job_id': jid, 'company': row['company'], 'title': row['title'],
                   'url': row['url'], 'eligibility': row.get('eligibility'),
                   'current_eligibility': evaluation.get('decision'),
                   'current_application_status': application.get('status'),
                   'attempt_count': len(attempts)}
        if attempts:
            latest = attempts[-1]
            outcome.update(status=latest['status'], reason=latest.get('reason'),
                           attempt_id=latest['id'],
                           model_cost_usd=sum(float(a.get('cost_usd') or 0) for a in attempts))
            answers, issues = answers_from_trace(store.get_apply_trace(latest['id']))
            outcome.update(answers=answers, review_details=issues)
            quality = [e['payload'] for e in store.get_apply_trace(latest['id']) if e['event_type']=='answer_quality_flag']
            if quality:
                outcome['answer_quality_flags'] = quality
            corrections = [e['payload'] for e in store.get_apply_trace(latest['id'])
                           if e['event_type'] == 'application_correction_requested']
            if corrections:
                outcome['correction_requests'] = corrections
            artifacts = [e['payload'] for e in store.get_apply_trace(latest['id'])
                         if e['event_type'] == 'submission_artifact_flag']
            if artifacts:
                outcome['submission_artifact_flags'] = artifacts
        elif not evaluation and liveness.get('status') != 'closed' and job and not store.get_facts(jid):
            outcome.update(status='retrieval_or_extraction_failed', reason=job.fetch_error or 'No grounded job facts available; application not started')
        elif row.get('eligibility') != 'eligible':
            outcome.update(status='not_eligible_or_closed', reason=row.get('eligibility_reasons'))
        elif liveness.get('status') == 'closed':
            outcome.update(status='posting_closed', reason=liveness.get('detail'))
        elif _matches_prior_role(row['company'], row['title'], roles):
            outcome.update(status='protected_similar_prior_role', reason='Duplicate protection: similar role at company already attempted')
        elif job and canonical_ats_identity(job) in identities and canonical_ats_identity(job):
            outcome.update(status='protected_prior_identity', reason='Duplicate protection: same ATS requisition already attempted')
        else:
            outcome.update(status=application.get('status', 'not_attempted'),
                           reason=application.get('reason'))
        if row.get('eligibility') == 'eligible' and evaluation.get('decision') == 'ineligible':
            if outcome['status'] == 'applied':
                outcome['current_policy_warning'] = evaluation.get('reasons') or []
            elif outcome['status'] != 'in_progress':
                outcome['latest_attempt_status'] = outcome['status']
                outcome['status'] = 'excluded_by_current_profile'
                outcome['reason'] = evaluation.get('reasons') or []
        if outcome['status'] in {'review', 'failed', 'infra_error', 'skipped'}:
            category, next_step = review_triage(outcome)
            outcome.update(review_category=category, next_step=next_step)
        jobs.append(outcome)
    return {'generated_at': datetime.now(timezone.utc).isoformat(),
            'scope': f"{len(manifest['jobs'])} jobs ingested {manifest.get('date', 'unknown date')}",
            'pipeline_status': manifest.get('status'),
            'blocker': manifest.get('blocker'),
            'counts': dict(Counter(j['status'] for j in jobs)),
            'review_categories': dict(Counter(j['review_category'] for j in jobs if j.get('review_category'))),
            'model_cost_usd': sum(j.get('model_cost_usd', 0) for j in jobs),
            'notes': ['Answers reconstructed only from successful tools; failed proposals are not counted as answers.',
                      'Current policy exclusions supersede retry holds; confirmed historical submissions remain applied.',
                      'Question labels may be truncated by browser snapshots.',
                      'This report omits identity and demographic answers.'],
            'jobs': jobs}


def markdown_report(report):
    lines = [f"# Application outcomes — {report.get('scope', 'ingestion batch')}", '',
             f"Updated: {report['generated_at']}", '',
             '## Totals', '']
    flagged = [job for job in report['jobs'] if job.get('answer_quality_flags') or job.get('submission_artifact_flags')]
    if flagged:
        lines[4:4] = ['## Submitted applications requiring attention', '',
                      f"Pipeline status: {report.get('pipeline_status') or 'See manifest'}", '']
        warnings = []
        for job in flagged:
            for issue in job.get('answer_quality_flags', []):
                warnings += [f"- {job['company']} — {job['title']}: {issue.get('question')}",
                             f"  Entered: {issue.get('entered')}. Correct/profile: {issue.get('correct_answer') or issue.get('grounded_profile')}. {issue.get('issue')}"]
            for correction in job.get('correction_requests', []):
                warnings.append(f"  Later correction request: {correction.get('status')}. {correction.get('acknowledgement')}. ATS answer update remains unconfirmed.")
            for artifact in job.get('submission_artifact_flags', []):
                warnings.append(f"- {job['company']} — {job['title']}: attachment check needed. {artifact.get('issue')}")
        lines[8:8] = warnings + ['']
    lines += [f'- {status}: {count}' for status, count in report['counts'].items()]
    lines += ['', f"Recorded application model cost: ${report['model_cost_usd']:.4f}", '',
              'Only confirmed submissions count as applied. An unconfirmed submit must not be retried automatically.', '',
              'Required narrative composition is user-authorized; optional free-form questions are skipped. Policy requires grounded legal and qualification facts; any recorded violations are flagged above.', '',
              '## Confirmed submissions', '']
    for job in report['jobs']:
        if job['status'] == 'applied':
            warning = ' (historical submission; now excluded by current profile)' if job.get('current_policy_warning') else ''
            lines.append(f"- [{job['company']} — {job['title']}]({job['url']}){warning}")
            for issue in job.get('answer_quality_flags', []):
                lines.append(f"  - Submitted answer needs review: {issue.get('question')}: entered {issue.get('entered')}; profile {issue.get('grounded_profile')}. {issue.get('issue')}")
            for artifact in job.get('submission_artifact_flags', []):
                lines.append(f"  - Attachment check needed: {artifact.get('issue')}")
    lines += ['', '## Review categories', '']
    lines += [f'- {category}: {count}' for category, count in report.get('review_categories', {}).items()]
    lines += ['', 'These categories describe the stop mechanism, not a claim that every fix is already implemented or verified.', '',
              '## Problems and review queue', '']
    for job in report['jobs']:
        if job['status'] not in {'review', 'failed', 'infra_error', 'skipped'}:
            continue
        lines += [f"### {job['company']} — {job['title']}", '',
                  f"Status: {job['status']}", '', f"Reason: {job.get('reason') or 'Unknown'}", '',
                  f"Triage: {job.get('review_category', 'unclassified')}", '',
                  f"Next step: {job.get('next_step', 'Inspect trace')}", '']
        seen = set()
        for detail in job.get('review_details', []):
            question = detail.get('question') or detail.get('label')
            reason = detail.get('error') or detail.get('reason') or detail.get('issue')
            if question and question not in seen:
                seen.add(question)
                lines.append(f"- Question: {str(question).replace(chr(10), ' / ')}")
                if reason:
                    lines.append(f"  Detail: {reason}")
        if not seen:
            lines.append('- See the reason and recorded trace for the hold; no reliable question label was captured.')
        lines += ['', f"[Job posting]({job['url']})", '']
    lines += ['## Protected / excluded / unattempted', '']
    for job in report['jobs']:
        if job['status'] not in {'applied', 'review', 'failed', 'infra_error', 'skipped'}:
            lines.append(f"- {job['company']} — {job['title']}: {job['status']}; {job.get('reason') or ''}")
    lines += ['', '## Evidence notes', ''] + [f'- {note}' for note in report['notes']]
    return '\n'.join(lines) + '\n'


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', default='data/batches/ingest-20261022.json')
    parser.add_argument('--output', default='data/batches/application-audit-20261022.json')
    parser.add_argument('--markdown-output')
    args = parser.parse_args()
    report = build_report(json.loads(Path(args.manifest).read_text()))
    Path(args.output).write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
    if args.markdown_output:
        Path(args.markdown_output).write_text(markdown_report(report))
    print(json.dumps({k: v for k, v in report.items() if k != 'jobs'}, indent=2))
