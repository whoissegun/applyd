# Jev assessment for applyd

Research date: 2026-09-21. Synthetic API evaluations were followed by an opt-in
hybrid browser pilot. That pilot retained Kimi as the browser driver and was
stopped after a submitted sponsorship answer contradicted the profile. Its
results do not validate Jev as an application controller.

A separate `APPLYD_JEV_CONTROLLER=true` prototype now classifies bounded actions
without Kimi fallback. It supports a narrow native-control/Ashby-container
surface and routes legal/custom/unknown required questions to review. Live use
requires a fresh per-job dry-run manifest; see setup.md and
scripts/validate_jev_job.py.
Do not infer whole-form accuracy from the synthetic decision scores below.

## Controller update — 2026-09-22

Python now owns field ordering. Jev classifies each observed target into a
profile field or operation, and execution requires agreement with Python's
exact-label binding. Human-readable field criteria replaced opaque action
selection. This passed all ten existing development fixtures using the real
API (five ordinary forms completed to the non-submitting boundary, five unsafe
forms stopped in Python). Earlier runs failed; the latest result is after
prompt development, not an independent held-out accuracy estimate.

A separate, predeclared 24-label policy check returned 22/24 correct Jev
classifications. The two errors were `Employer email address` and `Manager
phone number`: Jev confidently mapped them to the applicant's fields. Python's
exact-label guard rejected both. Eighteen supported fields were admitted and
all six other labels blocked. Cost estimate: $0.000380982. This small,
author-labeled check is not independent production calibration.

The provisional threshold is now risk-specific: 0.90 for independently bound
native field fills/selections, 0.95 for upload/submission classification. A
semantic mismatch always blocks. A matching job/profile/resume/code/form
manifest from a successful dry run is also required for any live canary; the
model cannot waive that requirement. Unknown legal facts and unsupported
controls remain review-only.

### Actual-form canary outcome

Auctor's Software Engineering Intern form completed a non-submitting validation
on 2026-09-22: four profile-bound contact fields, one runner-bound resume upload,
read-back checks, no communication-consent clicks, and zero Kimi browser calls.
The opt-in real canary then made exactly one submit attempt. Ashby explicitly
rejected it as possible spam; no submission was confirmed. A mailbox search
afterward found no Auctor confirmation email. The application remains `review`,
not `applied`, and was not retried. The other fourteen real applications were
paused because the first-success condition was not met.

The real canary made six Jev requests, estimated at $0.00016611; that excludes
dry runs, benchmarks, and the separately recorded Kimi resume tailoring.
This establishes a working bounded fill loop on one observed form, not a
successful application or a general accuracy/anti-spam claim.

## Bounded controller retry — 2026-09-21

Ten fictional forms were opened in a fresh local Chrome session with the real
TypeSafe API enabled. No employer pages, candidate files, application database,
or persistent Chrome profile were used. Kimi construction was forbidden by
the harness. No applications were submitted.

- Five ordinary forms exercised email, name, phone, city, and a native province
  dropdown. None reached the test-mode completion boundary.
- The initial action prompt omitted whether a field still needed a value.
  A diagnostic email case selected the intended action with confidence 0.48.
- Adding explicit pending-action metadata (without exposing profile values)
  improved confidence to 0.83–0.92 on those five cases. Every returned choice
  was the intended action, but the unchanged provisional 0.95 gate blocked it.
- Five unsupported/safety cases (sponsorship, checked recruiting consent, NDA,
  historical claims, custom widget) were stopped deterministically before an API
  request. These are Python guard passes, **not** Jev classification successes.

Thus the ten-form acceptance result is 5/10, with 0/5 ordinary forms completed.
This demonstrates fail-closed plumbing, not readiness or superior accuracy.
The confidence threshold needs a separately labeled calibration set; tuning on
these same fixtures would not be an independent validation. Multiple equally
valid action orders also need consideration before using choice confidence as
a safety criterion. Legal interpretation, custom widgets, full-form coverage,
and real submission remain unvalidated. The mocked full-browser regression
does reach the test boundary and proves execution/read-back without Kimi;
it must not be confused with the real-API result.

## Recommendation

Trial TypeSafe AI's Jev as an optional shadow classifier for role family,
seniority, and employment type. Do not replace Kimi, local embeddings, or
deterministic eligibility rules without task-specific evidence.

The most useful first question is whether a posting is software engineering,
machine learning, data, or another family. Ambiguous ML/data-platform roles
need explicit criteria and an unknown option. Exclusions remain personal
profile policy, never model-wide defaults.

## What the primary sources establish

- Jev returns bounded Choice, Score, and Noul decisions, rather than arbitrary
  prose. This suits classification but does not directly replace evidence-quote
  extraction, resume writing, or the current browser agent.
  [Primitives](https://docs.typesafe.ai/primitives)
- The documented model is `jev-1.13.0`, with text inputs and hosted API access.
  Listed pricing is $0.042 per million input tokens, with free output tokens.
  A hypothetical 1,000 classifications at 2,000 total input tokens each would
  therefore cost $0.084, excluding retries and extra requests. That is an
  arithmetic illustration, not a measured applyd workload.
  [Models](https://docs.typesafe.ai/models)
- Vendor workflow evaluations compare against other models' reference
  probabilities, not independently human-labeled applyd jobs. The vendor also
  acknowledges favorable demo conditions and potential evaluation bias.
  Its type-safety guarantee is not a guarantee of semantic correctness.
  [Launch and evaluation methodology](https://typesafe.ai/blog/introducing-system-one-models-and-jev)
- Documented weaknesses include negation/indirection, numeric and date reasoning,
  irrelevant context, and adversarial input. Job descriptions are untrusted
  external content, so that last limitation matters here.
  [Known limitations](https://docs.typesafe.ai/model-jaggedness/jev-1.13)
- Confidence describes the distribution over answers. Thresholds must be
  validated on our workload, not interpreted as proven accuracy percentages.
  [Confidence](https://docs.typesafe.ai/confidence)
- The provider states it does not train on customer requests/responses; zero
  data retention is offered for enterprise customers. Do not assume standard
  accounts have zero retention. Keep an initial trial to public job text.
  [Models](https://docs.typesafe.ai/models), [Legal](https://docs.typesafe.ai/legal)

## Synthetic pilot completed

`scripts/benchmark_jev_kimi.py` contains 24 development fixtures: eight role
classifications, eight question-to-profile-field mappings, and eight answer
option choices. All data is fictional. Fixtures and expected labels were fixed
before the first run, but are not independently human-labeled or held-out.
Neither provider receives the expected labels. Both receive the same task
instructions, state, and answer options. Kimi uses JSON output with reasoning
disabled; Jev uses native Choice questions. This is a decomposed-decision
comparison, not a comparison against the entire existing Kimi browser agent.

| Measurement | Jev 1.13.0 | Kimi K2.6 |
| --- | ---: | ---: |
| Role classification | 8/8 | 8/8 |
| Question-to-field mapping | 8/8 | 8/8 |
| Answer-option selection | 8/8 | 8/8 |
| Missing-fact cases correctly sent to review | 3/3 | 3/3 |
| Total request elapsed time, three batches | 0.941 s | 9.400 s |
| Cost | $0.000240618 estimated | $0.0038454101 reported |

Jev cost is calculated from reported input tokens and its published rate, not
an invoice. Kimi cost is the OpenRouter response's usage cost, including its
actual routing/cache behavior. This one run suggests roughly 10x lower request
time and 16x lower model cost for these fixtures. It does not establish a
production speedup, accuracy improvement, calibrated confidence threshold, or
whole-form reliability. Browser waiting, extra stages, and fallback costs are
not included. No reruns or prompt tuning were used to obtain these results.

Local raw report: `data/benchmarks/jev-kimi-20260921-142253.json` (Git-ignored).
No API key is written to the report or benchmark source.

Validate fixtures without network calls:

```bash
PYTHONPATH=src .venv/bin/python scripts/benchmark_jev_kimi.py
```

For an explicitly paid rerun, configure `TYPESAFE_API_KEY` and
`OPENROUTER_API_KEY` in your local ignored `.env` or environment, then append
`--run`. Each run makes at most six evaluation requests, without automatic
retries. Existing HTTP dependencies suffice; the TypeSafe SDK is not required.
This script never reads the candidate profile, resume, database, or browser.

## Extended synthetic pilot completed

The second run used 36 additional predeclared development cases, six per task.
Same model settings and scoring method as the first run. These are new synthetic
cases, not an independently labeled real-world held-out dataset.

| Task | Jev | Kimi |
| --- | ---: | ---: |
| Select an evidence passage that entails a claim | 6/6 | 6/6 |
| Select grounded resume bullets | 6/6 | 6/6 |
| Categorical relevance assessment | 6/6 | 6/6 |
| Interpret page/confirmation/error state | 6/6 | 6/6 |
| Harder answer-option mapping | 5/6 | 5/6 |
| Conservative duplicate-identity advice | 6/6 | 5/6 |
| Total | 35/36 | 34/36 |

Six request batches per provider took 1.982 seconds for Jev and 11.981 seconds
for Kimi. Jev's estimated cost was $0.000332934; Kimi reported $0.0043887332.
That is approximately 6x faster and 13x cheaper in this run, not a production
estimate. Local report: `data/benchmarks/jev-kimi-20260921-143853.json`.

The important failure was shared: a fictional profile said sponsorship is not
required **currently**, while the question asked about **now or any future
point**. Both chose No instead of review. Jev reported confidence 0.25 on that
choice. This is evidence to investigate abstention, not justification for
choosing a threshold after seeing the answer and claiming improved accuracy.
Temporal coverage must be checked separately from country and fact identity.

Kimi also labeled two similarly named roles in different cities as definitely
different postings when no posting IDs established that. Under the explicit
identity rubric, the answer should have been review. Existing deterministic
deduplication is not changed by this advisory experiment.

Reproduce fixture validation with `--extended`; add `--run` for twelve paid API
requests. Neither run tests live browser control, whole-form completion, free
text generation, numeric ranking, or full document extraction. Page-state cases
are static text observations. Source spans and resume bullets were supplied
as choices; retrieval and candidate generation costs were not tested. Tasks
already implemented exactly in code (IDs, dates, rendering, storage) remain
in code regardless of model accuracy.

## Larger evaluation, not yet executed

1. Human-label 300 public postings, including ambiguous SWE/ML/data boundaries,
   sparse descriptions, conflicting titles, and injected instructions.
2. Split development and held-out sets; pin the model version and criteria.
3. Compare Jev, existing Kimi extraction, and the deterministic title baseline.
   Measure per-family precision/recall, especially false exclusion of SWE/ML,
   abstention rate, confidence calibration, p50/p95 latency, and billed cost.
4. Run in shadow mode without changing eligibility, ranking, or applications.
   Preserve required verbatim evidence: a confident label alone cannot exclude
   a posting. Fall back to existing grounded extraction when uncertain.
5. Adopt only if held-out quality meets or exceeds the baseline with worthwhile
   measured savings. Keep legal facts, date arithmetic, uploads, navigation,
   submission confirmation, and safety limits under deterministic control.

Extend the same held-out approach to form questions and rendered options,
including compound questions, country/timeframe ambiguity, missing values,
long catalogs, and hostile instructions. Compare decomposed Kimi, Jev alone,
and Jev with Kimi fallback using identical guards. Evaluate complete forms in
offline browser fixtures before any opt-in, non-submitting live pilot.

Jev cannot repair stale DOM refs, missing candidate facts, CAPTCHA failures,
or dropped browser sessions. Those are separate engineering/provider issues.
