from __future__ import annotations

import json
import unittest
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from applyd.apply.tools import (
    _SNAPSHOT_JS,
    TOOL_DEFS,
    _date_profile_guard,
    _grounded_fill_value,
    fill_autocomplete,
    inspect_dropdowns,
    open_dropdown,
    pick_option,
    _profile_click_guard,
    _resume_upload_guard,
    _select_profile_guard,
    _match_option,
    _solve_brightdata_captcha,
    click,
    dispatch,
    select_option,
    snapshot,
    submit,
)
from applyd.apply.runner import (
    _max_turns_verdict,
    _missing_info_labels,
    _sanitize_tool_args,
    _sanitize_tool_result,
    _tool_signature,
    _normalize_report_status,
    _profile_already_answers,
    _terminal_tool_verdict,
    run_apply,
)
from applyd.discovery.routing import preferred_apply_url


class _Locator:
    def __init__(self, control):
        self.control = control

    def evaluate(self, *_args, **_kwargs):
        return self.control


class _Page:
    def __init__(self, control):
        self.control = control

    def locator(self, _selector):
        return self

    @property
    def first(self):
        return _Locator(self.control)


class _WaitPage:
    def wait_for_timeout(self, _milliseconds):
        return None


class ApplyToolBindingTests(unittest.TestCase):
    def test_final_turn_confirmation_enters_review_not_failed(self) -> None:
        status, note = _max_turns_verdict(24, 25)
        self.assertEqual(status, "review")
        self.assertIn("submission_confirmation_unacknowledged", note)
        self.assertEqual(
            _max_turns_verdict(None, 25),
            ("failed", "hit MAX_TURNS=25 without report_done"),
        )

    def test_missing_info_labels_are_extracted_for_profile_guard(self) -> None:
        self.assertEqual(
            _missing_info_labels("review:missing_info | field='GPA'"),
            ["GPA"],
        )
        self.assertEqual(
            _missing_info_labels(
                "review:missing_info | fields=Country of residence; Which state are you located in?"
            ),
            ["Country of residence", "Which state are you located in?"],
        )

    def test_trace_redacts_typed_values_and_snapshot_values(self) -> None:
        args = _sanitize_tool_args(
            "fill_many",
            {"fields": [{"ref": "r1", "value": "private@example.com"}]},
        )
        self.assertEqual(args, {"fields": [{"ref": "r1", "value_chars": 19}]})
        result = _sanitize_tool_result(
            "r1: [input/text *] 'Email' value='private@example.com'"
        )
        self.assertNotIn("private@example.com", result)
        self.assertIn("value='<redacted>'", result)

    def test_repeated_action_signature_is_order_independent(self) -> None:
        self.assertEqual(
            _tool_signature("fill", {"ref": "r1", "value": "x"}),
            _tool_signature("fill", {"value": "x", "ref": "r1"}),
        )

    def test_human_solvable_gates_normalize_to_review(self) -> None:
        self.assertEqual(
            _normalize_report_status("failed", "gated:captcha — timeout"),
            "review",
        )
        self.assertEqual(
            _normalize_report_status("skipped", "gated:email_verification"),
            "review",
        )

    def test_captcha_tool_failure_ends_without_another_model_turn(self) -> None:
        self.assertEqual(
            _terminal_tool_verdict(
                "submit", "error: captcha did not resolve within 90s"
            ),
            ("review", "gated:captcha"),
        )
        self.assertIsNone(_terminal_tool_verdict("fill", "error: timeout"))

    def test_greenhouse_apply_uses_embedded_form(self) -> None:
        url = preferred_apply_url(
            "simplifyjobs:anything",
            "https://boards.greenhouse.io/geotab/jobs/1234567",
            company="Geotab",
        )
        self.assertIn("job-boards.greenhouse.io/embed/job_app", url)
        self.assertIn("token=1234567", url)

    def test_report_done_supports_review_queue(self) -> None:
        report = next(
            item for item in TOOL_DEFS if item["function"]["name"] == "report_done"
        )
        statuses = report["function"]["parameters"]["properties"]["status"]["enum"]
        self.assertIn("review", statuses)

    def test_cover_letter_upload_tool_is_available(self) -> None:
        names = {item["function"]["name"] for item in TOOL_DEFS}
        self.assertIn("upload_cover_letter", names)

    def test_one_call_dropdown_tool_is_available(self) -> None:
        names = {item["function"]["name"] for item in TOOL_DEFS}
        self.assertIn("select_option", names)
        self.assertIn("inspect_dropdowns", names)

    def test_required_question_preflight_tool_is_available(self) -> None:
        names = {item["function"]["name"] for item in TOOL_DEFS}
        self.assertIn("preflight", names)

    def test_structured_profile_overrides_false_work_authorization_gap(self) -> None:
        profile = {
            "work_authorization": {
                "US": {"authorized": False, "requires_sponsorship": True}
            }
        }
        self.assertTrue(_profile_already_answers(
            "Are you legally authorized to work in the US?", profile
        ))
        self.assertTrue(_profile_already_answers(
            "Will you require immigration sponsorship, for example H-1B status?", profile
        ))
        self.assertFalse(_profile_already_answers(
            "Have you ever held a U.S. security clearance?", profile
        ))
        self.assertTrue(_profile_already_answers(
            "Will you now or in the future require sponsorship?",
            profile,
            job_locations=["Boston, MA"],
        ))
        self.assertTrue(_profile_already_answers(
            "Do you require employer sponsorship to work in the United States?",
            profile,
        ))
        self.assertTrue(_profile_already_answers(
            "Do you currently have the legal right to work in one of these locations?",
            profile,
            job_locations=["Toronto, Canada", "New York, NY"],
        ))

    def test_structured_profile_overrides_false_city_gap(self) -> None:
        self.assertTrue(_profile_already_answers(
            "Location (City)*", {"address_city": "Ottawa"}
        ))

    def test_structured_profile_overrides_street_and_current_company_gaps(self) -> None:
        profile = {
            "address_line1": "123 Main Street",
            "current_company": "Carleton University (student)",
        }
        self.assertTrue(_profile_already_answers("Street Address:*", profile))
        self.assertTrue(_profile_already_answers("Current company", profile))

    def test_profile_answers_graduation_date_and_remote_city_question(self) -> None:
        profile = {
            "expected_grad_date": "2027-04",
            "address_city": "Ottawa",
            "address_region": "Ontario",
            "address_country": "Canada",
        }
        self.assertTrue(_profile_already_answers("What is your graduation date?", profile))
        self.assertTrue(_profile_already_answers(
            "Are you located in the greater Seattle area?", profile
        ))

    def test_preferred_first_name_uses_profile_first_name(self) -> None:
        self.assertTrue(_profile_already_answers(
            "Preferred First Name*", {"first_name": "Jane"}
        ))

    def test_full_name_gap_uses_grounded_profile_name(self) -> None:
        self.assertTrue(_profile_already_answers(
            "Full name", {"first_name": "Jane", "last_name": "Doe"}
        ))
        self.assertFalse(_profile_already_answers(
            "Full name", {"first_name": "Jane"}
        ))

    def test_education_labels_use_structured_profile(self) -> None:
        profile = {
            "school": "Carleton University",
            "degree": "Bachelor of Computer Science",
            "major": "Computer Science",
        }
        self.assertTrue(_profile_already_answers("Education", profile))
        self.assertTrue(_profile_already_answers("Discipline*", profile))

    def test_motivation_and_hourly_rate_are_not_false_profile_gaps(self) -> None:
        self.assertTrue(_profile_already_answers(
            "Why do you want to join LiveFlow?", {}
        ))
        self.assertTrue(_profile_already_answers(
            "What is your expected hourly rate?*",
            {"salary_expectation": {
                "strategy": "use_posted_range_or_negotiable",
                "fallback": "Negotiable",
            }},
        ))
        self.assertFalse(_profile_already_answers(
            "Upload a video explaining why you want to join us", {}
        ))
        self.assertTrue(_profile_already_answers(
            "Which languages do you speak?",
            {"spoken_languages": ["English"]},
        ))

    def test_structured_profile_overrides_country_and_region_gaps(self) -> None:
        profile = {"address_country": "Canada", "address_region": "Ontario"}
        self.assertTrue(_profile_already_answers(
            "Country (field0 dropdown): current residence", profile
        ))
        self.assertTrue(_profile_already_answers(
            "Which state are you located in?", profile
        ))

    def test_location_specific_visa_question_uses_job_region(self) -> None:
        profile = {"work_authorization": {
            "UK": {"authorized": False, "requires_sponsorship": True}
        }}
        self.assertTrue(_profile_already_answers(
            "Would you now or in the future require a visa for employment "
            "for where this job is based?",
            profile,
            job_locations=["London, United Kingdom"],
        ))

    def test_resume_overrides_false_employment_history_gap(self) -> None:
        self.assertTrue(_profile_already_answers(
            "Who is your current or previous employer?*",
            {},
            resume_text="Lyft, Software Developer Intern",
        ))

    def test_false_location_premise_is_answerable(self) -> None:
        self.assertTrue(_profile_already_answers(
            "If located in the US, in what city and state do you reside?*",
            {"address_country": "Canada", "address_country_code": "CA"},
        ))

    def test_target_company_history_and_family_defaults_are_answerable(self) -> None:
        profile = {"background_defaults": {
            "never_employed_by_target_unless_listed_on_resume": True,
            "no_immediate_family_at_target_company": True,
        }}
        self.assertTrue(_profile_already_answers(
            "Have you ever been employed by SimpliSafe?",
            profile,
            company="SimpliSafe",
            resume_text="Lyft and Shopify",
        ))
        self.assertTrue(_profile_already_answers(
            "Does an immediate family member work at SimpliSafe?", profile
        ))

    def test_clearance_and_export_control_are_structured(self) -> None:
        profile = {
            "security_clearance": {
                "US": {"held": False, "past_level": "none", "eligible": False}
            },
            "export_control": {
                "us_person": False, "classification": "foreign_person"
            },
        }
        self.assertTrue(_profile_already_answers(
            "Are you eligible to obtain a U.S. security clearance?", profile
        ))
        self.assertTrue(_profile_already_answers(
            "What U.S. security clearance have you held in the past?", profile
        ))
        self.assertTrue(_profile_already_answers(
            "EXPORT CONTROLS - select your status", profile
        ))

    def test_available_now_date_is_runner_grounded(self) -> None:
        page = MagicMock()
        locator = MagicMock()
        page.locator.return_value.first = locator
        locator.evaluate.return_value = "When can you start a new role?"
        value, note = _grounded_fill_value(
            page,
            "r12",
            "2025-06-01",
            {"earliest_start_date": "immediately"},
        )
        self.assertRegex(value, r"^\d{4}-\d{2}-\d{2}$")
        self.assertNotEqual(value, "2025-06-01")
        self.assertIn("grounded start date", note or "")

    def test_when_are_you_available_is_runner_grounded(self) -> None:
        page = MagicMock()
        locator = MagicMock()
        page.locator.return_value.first = locator
        locator.evaluate.return_value = "When are you available to begin?"
        value, note = _grounded_fill_value(
            page, "r12", "Eventually", {"earliest_start_date": "now"}
        )
        self.assertRegex(value, r"^\d{4}-\d{2}-\d{2}$")
        self.assertIn("grounded start date", note or "")
        self.assertTrue(_profile_already_answers(
            "When are you available to begin?",
            {"earliest_start_date": "now"},
        ))

    def test_ambiguous_start_date_is_not_treated_as_availability(self) -> None:
        page = MagicMock()
        locator = MagicMock()
        page.locator.return_value.first = locator
        locator.evaluate.return_value = "Start date year"
        value, note = _grounded_fill_value(
            page,
            "r12",
            "2022",
            {"earliest_start_date": "immediately"},
        )
        self.assertEqual(value, "2022")
        self.assertIsNone(note)

    def test_instabase_cohort_dates_cannot_replace_grounded_dates(self) -> None:
        profile = {
            "expected_grad_date": "2027-04",
            "earliest_start_date": "immediately",
        }
        self.assertIn("contradicts grounded graduation date", _date_profile_guard(
            "Graduation Date", "July 2026", profile,
            today=date(2026, 9, 10),
        ) or "")
        self.assertIsNone(_date_profile_guard(
            "Graduation Date", "April 2027", profile,
            today=date(2026, 9, 10),
        ))
        self.assertIn("before the current month", _date_profile_guard(
            "Target Start Date", "June 2026", profile,
            today=date(2026, 9, 10),
        ) or "")
        self.assertIsNone(_date_profile_guard(
            "Target Start Date", "October 2026", profile,
            today=date(2026, 9, 10),
        ))
        self.assertIn("contradicts grounded graduation date", _date_profile_guard(
            "Are you graduating Summer of 2027?", "Yes", profile,
            today=date(2026, 9, 10),
        ) or "")
        self.assertIsNone(_date_profile_guard(
            "Are you graduating Summer of 2027?", "No", profile,
            today=date(2026, 9, 10),
        ))

    def test_maven_boolean_start_date_uses_date_in_question(self) -> None:
        profile = {"earliest_start_date": "immediately"}
        label = "Are you available to start from Monday 6th September 2027?"
        self.assertIsNone(_date_profile_guard(
            label, "Yes", profile, today=date(2026, 9, 10),
        ))
        self.assertIn("contradicts grounded availability", _date_profile_guard(
            label, "No", profile, today=date(2026, 9, 10),
        ) or "")
        self.assertIn("contradicts grounded availability", _date_profile_guard(
            "Are you available to start 6th September 2025?",
            "Yes",
            profile,
            today=date(2026, 9, 10),
        ) or "")

    @patch("applyd.apply.tools._ref_locator")
    def test_raw_pick_option_cannot_bypass_date_guard(self, ref_locator) -> None:
        locator = MagicMock()
        locator.evaluate.return_value = "div"
        locator.inner_text.return_value = "July 2026"
        ref_locator.return_value = locator
        page = MagicMock()
        page.evaluate.return_value = "Graduation Date"
        result = pick_option(page, "o3", {
            "expected_grad_date": "2027-04",
        })
        self.assertIn("contradicts grounded graduation date", result)
        locator.click.assert_not_called()

    def test_name_pronunciation_cannot_be_invented(self) -> None:
        page = MagicMock()
        locator = MagicMock()
        page.locator.return_value.first = locator
        locator.evaluate.return_value = "Name pronunciation"
        with self.assertRaisesRegex(ValueError, "not in the structured profile"):
            _grounded_fill_value(page, "r6", "invented phonetics", {
                "first_name": "Jane",
            })
        value, note = _grounded_fill_value(page, "r6", "wrong", {
            "name_pronunciation": "explicit pronunciation",
        })
        self.assertEqual(value, "explicit pronunciation")
        self.assertIn("grounded name pronunciation", note or "")

    def test_onsite_negative_option_is_blocked_when_profile_is_willing(self) -> None:
        page = MagicMock()
        locator = MagicMock()
        page.locator.return_value.first = locator
        locator.get_attribute.side_effect = lambda name: {
            "data-applyd-question": "Are you able to work from our NYC office 5 days per week?",
            "data-applyd-option": "No",
        }.get(name)
        result = _profile_click_guard(page, "r13", {
            "employment_preferences": {
                "willing_to_relocate": True,
                "willing_to_work_onsite": True,
            }
        })
        self.assertIn("refused contradictory option", result or "")

    def test_office_frequency_and_any_role_preflight_use_profile_preferences(self) -> None:
        profile = {"employment_preferences": {
            "willing_to_work_onsite": True,
            "willing_to_work_any_onsite_schedule": True,
            "accept_any_role_option": True,
        }}
        self.assertTrue(_profile_already_answers(
            "Are you happy to work from our office at least 3 days a week?",
            profile,
        ))
        self.assertTrue(_profile_already_answers(
            "What role are you applying for?",
            profile,
        ))

    def test_generic_optional_upload_gap_is_overridden_by_runner_resume(self) -> None:
        self.assertTrue(_profile_already_answers(
            "File upload areas", {}, resume_text="grounded resume latex"
        ))

    def test_exact_resume_gap_is_overridden_by_runner_resume(self) -> None:
        self.assertTrue(_profile_already_answers(
            "Resume", {}, resume_text="grounded resume latex"
        ))

    def test_structured_profile_overrides_false_relocation_gap(self) -> None:
        profile = {
            "employment_preferences": {"willing_to_relocate": True}
        }
        self.assertTrue(_profile_already_answers(
            "Please confirm that you are willing to relocate to San Francisco", profile
        ))

    def test_dropdown_matching_is_exact_then_unambiguous(self) -> None:
        options = [
            {"ref": "o0", "text": "No"},
            {"ref": "o1", "text": "Yes"},
            {"ref": "o2", "text": "Black or African American"},
        ]
        self.assertEqual(_match_option(options, "YES")["ref"], "o1")
        self.assertEqual(_match_option(options, "Black")["ref"], "o2")
        self.assertIsNone(_match_option([
            {"ref": "o0", "text": "Yes, now"},
            {"ref": "o1", "text": "Yes, later"},
        ], "Yes"))

    def test_dropdown_matching_handles_safe_ats_wording_variants(self) -> None:
        self.assertEqual(_match_option([
            {"ref": "o0", "text": "Associate's Degree"},
            {"ref": "o1", "text": "Bachelor's Degree"},
            {"ref": "o2", "text": "Doctor of Philosophy (Ph.D.)"},
        ], "Bachelor of Computer Science")["ref"], "o1")
        self.assertEqual(_match_option([
            {"ref": "o0", "text": "Employee Referral"},
            {"ref": "o1", "text": "SharkNinja Career Website"},
        ], "Company careers page")["ref"], "o1")
        self.assertEqual(_match_option([
            {"ref": "o0", "text": "Referral"},
            {"ref": "o1", "text": "ID.me Careers page"},
        ], "Company careers page")["ref"], "o1")
        self.assertEqual(_match_option([
            {"ref": "o0", "text": "Male"},
            {"ref": "o1", "text": "Decline To Self Identify"},
        ], "Prefer not to say")["ref"], "o1")

    @patch("applyd.apply.tools._ref_locator")
    def test_open_dropdown_is_idempotent_when_options_are_visible(
        self, ref_locator
    ) -> None:
        locator = MagicMock()
        locator.evaluate.return_value = "input"
        locator.get_attribute.return_value = "1"
        ref_locator.return_value = locator
        page = MagicMock()
        page.evaluate.return_value = [{"ref": "o0", "text": "Carleton University"}]
        result = open_dropdown(page, "r16")
        self.assertIn("already open", result)
        locator.click.assert_not_called()

    @patch("applyd.apply.tools._read_options")
    @patch("applyd.apply.tools.open_dropdown")
    @patch("applyd.apply.tools._ref_locator")
    def test_inspect_dropdowns_returns_labels_and_closes_each_menu(
        self, ref_locator, open_mock, read_options
    ) -> None:
        open_mock.return_value = "opened"
        read_options.side_effect = [
            [{"ref": "o0", "text": "Yes"}, {"ref": "o1", "text": "No"}],
            [{"ref": "o0", "text": "LinkedIn"}],
        ]
        locator = MagicMock()
        locator.evaluate.side_effect = ["input", None, "input", None]
        ref_locator.return_value = locator
        page = MagicMock()
        result = inspect_dropdowns(page, ["r25", "r38"])
        self.assertIn("r25: ['Yes', 'No']", result)
        self.assertIn("r38: ['LinkedIn']", result)
        self.assertEqual(open_mock.call_count, 2)
        self.assertEqual(page.keyboard.press.call_count, 2)

    @patch("applyd.apply.tools.pick_option", return_value="ok: picked o7")
    @patch("applyd.apply.tools._read_options")
    @patch("applyd.apply.tools.open_dropdown")
    @patch("applyd.apply.tools._ref_locator")
    def test_select_option_reuses_open_greenhouse_options(
        self, ref_locator, open_mock, read_options, _pick
    ) -> None:
        locator = MagicMock()
        locator.get_attribute.return_value = "1"
        ref_locator.return_value = locator
        read_options.return_value = [
            {"ref": "o7", "text": "Carleton University"}
        ]
        result = select_option(MagicMock(), "r16", "Carleton University")
        self.assertIn("selected 'Carleton University'", result)
        open_mock.assert_not_called()

    @patch("applyd.apply.tools.pick_option", return_value="ok: picked o0")
    @patch("applyd.apply.tools._ref_locator")
    def test_fill_autocomplete_accepts_react_picked_value(
        self, ref_locator, _pick
    ) -> None:
        locator = MagicMock()
        locator.evaluate.side_effect = [False, None, "Ottawa, Ontario, Canada"]
        ref_locator.return_value = locator
        page = MagicMock()
        page.evaluate.return_value = [
            {"ref": "o0", "text": "Ottawa, Ontario, Canada"}
        ]
        result = fill_autocomplete(page, "r9", "Ottawa")
        self.assertIn("picked", result)

    def test_accepted_report_done_stops_later_batched_tool_calls(self) -> None:
        def response(*calls):
            tool_calls = [
                SimpleNamespace(
                    id=f"call-{index}",
                    function=SimpleNamespace(
                        name=name, arguments=json.dumps(arguments)
                    ),
                )
                for index, (name, arguments) in enumerate(calls)
            ]
            message = MagicMock()
            message.tool_calls = tool_calls
            message.content = None
            message.model_dump.return_value = {"role": "assistant"}
            return SimpleNamespace(
                usage=SimpleNamespace(cost=0),
                choices=[SimpleNamespace(message=message)],
            )

        client = MagicMock()
        client.chat.completions.create.side_effect = [
            response(("navigate", {})),
            response(("snapshot", {})),
            response(
                ("report_done", {
                    "status": "review", "note": "review:manual_artifact"
                }),
                ("fill", {"ref": "r1", "value": "must not execute"}),
            ),
        ]

        @contextmanager
        def fake_browser(*_args, **_kwargs):
            yield MagicMock()

        with patch("applyd.apply.runner._make_client", return_value=client), patch(
            "applyd.apply.runner.browser_page", fake_browser
        ), patch(
            "applyd.apply.runner.dispatch",
            side_effect=["ok: loaded", "r1: [input/text *] 'Name'"],
        ) as dispatch_mock, patch(
            "applyd.apply.runner._start_hard_watchdog"
        ) as watchdog, patch(
            "applyd.apply.runner.load_env"
        ), patch(
            "applyd.apply.runner.build_code_reader", return_value=None
        ):
            watchdog.return_value.set.return_value = None
            result = run_apply(
                job_id="job-1", company="Example", title="Engineer",
                job_url="https://example.test/job", resume_pdf_path="resume.pdf",
                profile_md="{}", test_mode=False,
            )

        self.assertEqual(result["status"], "review")
        self.assertEqual(dispatch_mock.call_count, 2)

    def test_submit_confirmation_overrides_incorrect_model_downgrade(self) -> None:
        def response(*calls):
            tool_calls = [
                SimpleNamespace(
                    id=f"call-{index}",
                    function=SimpleNamespace(
                        name=name, arguments=json.dumps(arguments)
                    ),
                )
                for index, (name, arguments) in enumerate(calls)
            ]
            message = MagicMock()
            message.tool_calls = tool_calls
            message.content = None
            message.model_dump.return_value = {"role": "assistant"}
            return SimpleNamespace(
                usage=SimpleNamespace(cost=0),
                choices=[SimpleNamespace(message=message)],
            )

        client = MagicMock()
        client.chat.completions.create.side_effect = [
            response(("navigate", {})),
            response(("snapshot", {})),
            response(("preflight", {
                "answerable_required_labels": ["Name"], "missing_fields": []
            })),
            response(("submit", {"ref": "r9"})),
            response(("report_done", {
                "status": "skipped", "note": "gated:email_verification"
            })),
        ]

        @contextmanager
        def fake_browser(*_args, **_kwargs):
            yield MagicMock()

        with patch("applyd.apply.runner._make_client", return_value=client), patch(
            "applyd.apply.runner.browser_page", fake_browser
        ), patch(
            "applyd.apply.runner.dispatch",
            side_effect=[
                "ok: loaded",
                "r9: [button/submit *] 'Submit'",
                "ok: submission_confirmed via r9 after email verification",
            ],
        ), patch(
            "applyd.apply.runner._start_hard_watchdog"
        ) as watchdog, patch(
            "applyd.apply.runner.load_env"
        ), patch(
            "applyd.apply.runner.build_code_reader", return_value=None
        ):
            watchdog.return_value.set.return_value = None
            result = run_apply(
                job_id="job-2", company="Example", title="Engineer",
                job_url="https://example.test/job", resume_pdf_path="resume.pdf",
                profile_md="{}", test_mode=False,
            )

        self.assertEqual(result["status"], "applied")
        self.assertIn("submission_confirmed", result["note"])

    @patch("applyd.apply.tools.pick_option", return_value="ok: picked o0")
    @patch("applyd.apply.tools._ref_locator")
    @patch("applyd.apply.tools._read_options")
    @patch("applyd.apply.tools.open_dropdown", return_value="opened r4")
    def test_select_option_inspects_before_typing(
        self, _open, read_options, ref_locator, pick_option_mock
    ) -> None:
        read_options.return_value = [
            {"ref": "o0", "text": "No"},
            {"ref": "o1", "text": "Yes"},
        ]
        result = select_option(MagicMock(), "r4", "Yes")
        self.assertIn("selected 'Yes'", result)
        ref_locator.return_value.fill.assert_not_called()
        pick_option_mock.assert_called_once()

    @patch("applyd.apply.tools.pick_option", return_value="ok: picked o8")
    @patch("applyd.apply.tools._ref_locator")
    @patch("applyd.apply.tools._read_options")
    @patch("applyd.apply.tools.open_dropdown", return_value="opened r4")
    def test_select_option_searches_only_after_initial_options_miss(
        self, _open, read_options, ref_locator, pick_option_mock
    ) -> None:
        read_options.side_effect = [
            [{"ref": "o0", "text": "Aalborg University"}],
            [{"ref": "o8", "text": "Carleton University"}],
        ]
        locator = MagicMock()
        locator.evaluate.return_value = "input"
        locator.get_attribute.return_value = "combobox"
        ref_locator.return_value = locator
        page = MagicMock()
        result = select_option(page, "r4", "Carleton University")
        self.assertIn("selected 'Carleton University'", result)
        locator.click.assert_called_once_with(timeout=8000)
        locator.fill.assert_called_once_with("", timeout=8000)
        locator.press_sequentially.assert_called_once_with(
            "Carleton University", delay=35, timeout=20000
        )
        self.assertEqual(read_options.call_count, 2)
        pick_option_mock.assert_called_once()

    @patch("applyd.apply.tools.pick_option")
    @patch("applyd.apply.tools._ref_locator")
    @patch("applyd.apply.tools._read_options")
    @patch("applyd.apply.tools.open_dropdown", return_value="opened r4")
    def test_failed_greenhouse_catalog_search_clears_filter_for_fallback(
        self, _open, read_options, ref_locator, pick_option_mock
    ) -> None:
        read_options.side_effect = [
            [{"ref": "o0", "text": "Abertay University"}],
            [],
        ]
        locator = MagicMock()
        locator.evaluate.return_value = "input"
        locator.get_attribute.return_value = "combobox"
        ref_locator.return_value = locator
        page = MagicMock()

        result = select_option(page, "r29", "Carleton University")

        self.assertIn("no unambiguous match", result)
        self.assertEqual(locator.fill.call_count, 2)
        locator.fill.assert_called_with("", timeout=8000)
        page.wait_for_timeout.assert_any_call(500)
        pick_option_mock.assert_not_called()

    def test_snapshot_surfaces_blocking_captcha_frame(self) -> None:
        page = MagicMock()
        frame = MagicMock()
        frame.url = "https://geo.captcha-delivery.com/interstitial/?cid=redacted"
        page.frames = [frame]
        result = snapshot(page)
        self.assertIn("GATE DETECTED: CAPTCHA", result)
        page.evaluate.assert_not_called()

    def test_snapshot_surfaces_required_video_before_filling(self) -> None:
        page = MagicMock()
        page.frames = []
        page.evaluate.return_value = [{
            "ref": "r4", "role": "input", "type": "text",
            "label": "Record a video describing why you are the best person",
            "required": True, "value": "",
        }]
        self.assertIn("GATE DETECTED: MANUAL ARTIFACT", snapshot(page))

    def test_snapshot_recovers_lever_custom_question_labels(self) -> None:
        self.assertIn("li.application-question", _SNAPSHOT_JS)
        self.assertIn(".application-label", _SNAPSHOT_JS)

    def test_unknown_uk_authorization_selection_is_blocked(self) -> None:
        page = MagicMock()
        locator = MagicMock()
        page.locator.return_value.first = locator
        locator.get_attribute.return_value = (
            "Will you need a visa to work in the United Kingdom?"
        )
        result = _select_profile_guard(
            page, "r23", "No", {"work_authorization": {}}, "", ["London, UK"]
        )
        self.assertIn("not in the structured profile", result or "")

    def test_foreign_resident_cannot_select_a_fabricated_us_state(self) -> None:
        page = MagicMock()
        locator = MagicMock()
        page.locator.return_value.first = locator
        locator.get_attribute.return_value = "Which US state are you located in?"
        profile = {
            "address_region": "Ontario",
            "address_country": "Canada",
            "address_country_code": "CA",
        }
        result = _select_profile_guard(page, "r24", "Idaho", profile, "", [])
        self.assertIn("refused fabricated U.S. state", result or "")
        self.assertIsNone(_select_profile_guard(
            page, "r24", "Not in the US", profile, "", []
        ))

    def test_us_work_authorization_claim_must_match_profile(self) -> None:
        page = MagicMock()
        locator = MagicMock()
        page.locator.return_value.first = locator
        locator.get_attribute.return_value = (
            "Which best describes your current U.S. work authorization status?"
        )
        profile = {"work_authorization": {"US": {
            "authorized": False,
            "requires_sponsorship": True,
        }}}
        result = _select_profile_guard(
            page,
            "r24",
            "I am authorized to work in the U.S. through another status or "
            "work authorization (H1B, F1, OPT, CPT, etc).",
            profile,
            "",
            ["United States"],
        )
        self.assertIn("refused unsupported US work authorization claim", result or "")
        self.assertIsNone(_select_profile_guard(
            page,
            "r24",
            "I am not currently authorized to work in the United States.",
            profile,
            "",
            ["United States"],
        ))

    def test_sponsorship_yes_is_not_misread_as_authorization_yes(self) -> None:
        page = MagicMock()
        locator = MagicMock()
        locator.get_attribute.side_effect = lambda name: {
            "data-applyd-question": (
                "Will you now or in the future require employer sponsorship "
                "to work in the United States?"
            ),
            "data-applyd-label": (
                "Will you now or in the future require employer sponsorship "
                "to work in the United States? — Yes"
            ),
        }.get(name)
        page.locator.return_value.first = locator
        profile = {"work_authorization": {"US": {
            "authorized": False,
            "requires_sponsorship": True,
        }}}
        self.assertIsNone(_select_profile_guard(
            page, "r30", "Yes", profile, "", ["United States"]
        ))
        self.assertIn("no-sponsorship claim", _select_profile_guard(
            page, "r30", "No", profile, "", ["United States"]
        ) or "")
        locator.get_attribute.side_effect = lambda name: {
            "data-applyd-question": (
                "Will you now or in the future require employer sponsorship "
                "to work in the United States?"
            ),
            "data-applyd-option": "Yes",
            "data-applyd-label": (
                "Will you now or in the future require employer sponsorship "
                "to work in the United States? — Yes"
            ),
        }.get(name)
        self.assertIsNone(_profile_click_guard(page, "r30", profile))

    def test_pylon_short_no_option_uses_full_question_for_us_guard(self) -> None:
        page = MagicMock()
        locator = MagicMock()
        page.locator.return_value.first = locator
        locator.get_attribute.side_effect = lambda name: {
            "data-applyd-question": (
                "Will you now or in the future require sponsorship for employment "
                "visa status (e.g., H-1B visa status)?"
            ),
            "data-applyd-option": "No",
            "data-applyd-label": "No",
        }.get(name)
        profile = {"work_authorization": {"US": {
            "authorized": False,
            "requires_sponsorship": True,
        }}}
        result = _profile_click_guard(page, "r15", profile)
        self.assertIn("no-sponsorship claim", result or "")
        locator.get_attribute.side_effect = lambda name: {
            "data-applyd-question": (
                "Will you now or in the future require sponsorship for employment "
                "visa status (e.g., H-1B visa status)?"
            ),
            "data-applyd-option": "Yes",
            "data-applyd-label": "Yes",
        }.get(name)
        self.assertIsNone(_profile_click_guard(page, "r14", profile))

    def test_authorization_question_can_mention_sponsorship_help(self) -> None:
        page = MagicMock()
        locator = MagicMock()
        page.locator.return_value.first = locator
        question = (
            "Are you currently authorized to work in the United States? "
            "Retell AI DOES assist with visa sponsorship, so please feel free "
            "to share your current status."
        )
        locator.get_attribute.side_effect = lambda name: {
            "data-applyd-question": question,
            "data-applyd-option": "No",
            "data-applyd-label": f"{question} — No",
        }.get(name)
        profile = {"work_authorization": {"US": {
            "authorized": False,
            "requires_sponsorship": True,
        }}}
        self.assertIsNone(_profile_click_guard(page, "r11", profile))

    def test_lambda_sponsorship_question_is_not_treated_as_authorization(self) -> None:
        page = MagicMock()
        locator = MagicMock()
        page.locator.return_value.first = locator
        question = (
            "Lambda may agree to sponsor individuals to obtain work authorization "
            "such as H-1B, TN, or O-1 status. "
            "Will you now or in the future require sponsorship from Lambda to "
            "maintain employment authorization?"
        )
        profile = {"work_authorization": {"US": {
            "authorized": False,
            "requires_sponsorship": True,
        }}}
        locator.get_attribute.side_effect = lambda name: {
            "data-applyd-question": question,
            "data-applyd-option": "Yes",
            "data-applyd-label": f"{question} — Yes",
        }.get(name)
        self.assertIsNone(_profile_click_guard(page, "r18", profile))
        locator.get_attribute.side_effect = lambda name: {
            "data-applyd-question": question,
            "data-applyd-option": "No",
            "data-applyd-label": f"{question} — No",
        }.get(name)
        self.assertIn(
            "no-sponsorship claim",
            _profile_click_guard(page, "r19", profile) or "",
        )

    def test_authorization_dropdown_blocks_no_sponsorship_option(self) -> None:
        page = MagicMock()
        locator = MagicMock()
        page.locator.return_value.first = locator
        locator.get_attribute.side_effect = lambda name: {
            "data-applyd-question": (
                "Are you authorized to work lawfully in the United States?"
            ),
            "data-applyd-label": (
                "Are you authorized to work lawfully in the United States?"
            ),
        }.get(name)
        profile = {"work_authorization": {"US": {
            "authorized": False,
            "requires_sponsorship": True,
        }}}
        result = _select_profile_guard(
            page,
            "r24",
            "No, I do not require sponsorship either now OR in the future",
            profile,
            "",
            ["Seattle, WA, USA"],
        )
        self.assertIn("no-sponsorship claim", result or "")

    def test_generic_sponsorship_dropdown_uses_us_state_job_location(self) -> None:
        page = MagicMock()
        locator = MagicMock()
        page.locator.return_value.first = locator
        question = "Will you require any Visa sponsorship now or in the future?"
        locator.get_attribute.side_effect = lambda name: {
            "data-applyd-question": question,
            "data-applyd-label": question,
        }.get(name)
        profile = {"work_authorization": {"US": {
            "authorized": False,
            "requires_sponsorship": True,
        }}}

        result = _select_profile_guard(
            page, "r26", "No", profile, "", ["Chicago, IL"]
        )

        self.assertIn("false US no-sponsorship claim", result or "")

    def test_generic_sponsorship_radio_uses_us_state_job_location(self) -> None:
        page = MagicMock()
        locator = MagicMock()
        page.locator.return_value.first = locator
        question = "Will you require any Visa sponsorship now or in the future?"
        locator.get_attribute.side_effect = lambda name: {
            "data-applyd-question": question,
            "data-applyd-option": "No",
            "data-applyd-label": f"{question} — No",
        }.get(name)
        profile = {"work_authorization": {"US": {
            "authorized": False,
            "requires_sponsorship": True,
        }}}

        result = _profile_click_guard(
            page, "r26", profile, ["Chicago, IL"]
        )

        self.assertIn("false US no-sponsorship claim", result or "")

    @patch("applyd.apply.tools._ref_locator")
    def test_raw_pick_cannot_bypass_us_state_sponsorship_guard(
        self, ref_locator
    ) -> None:
        question = "Will you require any Visa sponsorship now or in the future?"
        option_locator = MagicMock()
        option_locator.evaluate.return_value = "div"
        option_locator.inner_text.return_value = "No"
        target_locator = MagicMock()
        target_locator.get_attribute.side_effect = lambda name: {
            "data-applyd-question": question,
            "data-applyd-label": question,
        }.get(name)
        ref_locator.side_effect = lambda _page, ref: (
            option_locator if ref == "o0" else target_locator
        )
        page = MagicMock()
        page.evaluate.return_value = {"ref": "r26", "label": question}
        profile = {"work_authorization": {"US": {
            "authorized": False,
            "requires_sponsorship": True,
        }}}

        result = pick_option(
            page, "o0", profile, job_locations=["Chicago, IL"]
        )

        self.assertIn("false US no-sponsorship claim", result or "")
        option_locator.click.assert_not_called()

    @patch("applyd.apply.tools._ref_locator")
    def test_raw_pick_rejects_unsupported_tn_status_claim(
        self, ref_locator
    ) -> None:
        question = "Do you need visa sponsorship now or in future?"
        option_locator = MagicMock()
        option_locator.evaluate.return_value = "div"
        option_locator.inner_text.return_value = (
            "I hold a TN visa and can work for Unlimited with a new employer "
            "petition, or I am a Canadian Citizen eligible for a TN1A visa"
        )
        target_locator = MagicMock()
        target_locator.get_attribute.side_effect = lambda name: {
            "data-applyd-question": question,
            "data-applyd-label": question,
        }.get(name)
        ref_locator.side_effect = lambda _page, ref: (
            option_locator if ref == "o6" else target_locator
        )
        page = MagicMock()
        page.evaluate.return_value = {"ref": "r12", "label": question}
        profile = {
            "citizenships": ["NG"],
            "work_authorization": {"US": {
                "authorized": False,
                "requires_sponsorship": True,
                "visa_status": None,
                "sponsorship_route": "H-1B",
            }},
        }

        result = pick_option(
            page, "o6", profile, job_locations=["San Francisco, CA"]
        )

        self.assertIn("unsupported US work authorization claim", result or "")
        option_locator.click.assert_not_called()

    def test_residence_radio_blocks_false_remote_city_claim(self) -> None:
        page = MagicMock()
        locator = MagicMock()
        page.locator.return_value.first = locator
        profile = {
            "address_city": "Ottawa",
            "address_region": "Ontario",
            "address_country": "Canada",
        }
        locator.get_attribute.side_effect = lambda name: {
            "data-applyd-question": "Are you located in the greater Seattle area?",
            "data-applyd-option": "Yes",
            "data-applyd-label": "Are you located in the greater Seattle area? — Yes",
        }.get(name)
        self.assertIn(
            "refused unsupported residence claim",
            _profile_click_guard(page, "r1", profile) or "",
        )
        locator.get_attribute.side_effect = lambda name: {
            "data-applyd-question": "Are you located in Ottawa?",
            "data-applyd-option": "No",
            "data-applyd-label": "Are you located in Ottawa? — No",
        }.get(name)
        self.assertIn(
            "refused false residence denial",
            _profile_click_guard(page, "r2", profile) or "",
        )

    def test_experience_range_requires_structured_years(self) -> None:
        page = MagicMock()
        locator = MagicMock()
        page.locator.return_value.first = locator
        locator.get_attribute.side_effect = lambda name: {
            "data-applyd-question": "How many years of Python experience do you have?",
            "data-applyd-label": "How many years of Python experience do you have?",
        }.get(name)
        self.assertIn(
            "experience duration is not grounded",
            _select_profile_guard(
                page, "r1", "3-5 years", {"first_name": "Jane"}, "", []
            ) or "",
        )

    def test_experience_range_must_match_structured_years(self) -> None:
        page = MagicMock()
        locator = MagicMock()
        page.locator.return_value.first = locator
        locator.get_attribute.side_effect = lambda name: {
            "data-applyd-question": (
                "How many years of professional software engineering experience do you have?"
            ),
            "data-applyd-label": (
                "How many years of professional software engineering experience do you have?"
            ),
        }.get(name)
        profile = {"years_professional_experience": 1}
        self.assertIsNone(
            _select_profile_guard(page, "r1", "1-3 years", profile, "", [])
        )
        self.assertIn(
            "refused experience range",
            _select_profile_guard(page, "r1", "3-5 years", profile, "", []) or "",
        )

    def test_degree_selection_allows_generic_level_but_blocks_substitution(self) -> None:
        page = MagicMock()
        locator = MagicMock()
        page.locator.return_value.first = locator
        locator.get_attribute.side_effect = lambda name: {
            "data-applyd-question": "Degree",
            "data-applyd-label": "Degree",
        }.get(name)
        profile = {"degree": "Bachelor of Computer Science"}
        self.assertIsNone(
            _select_profile_guard(page, "r1", "Bachelor's", profile, "", [])
        )
        self.assertIn(
            "refused credential substitution",
            _select_profile_guard(
                page, "r1", "Bachelor of Science", profile, "", []
            ) or "",
        )

    def test_grounded_address_and_current_company_fill_values(self) -> None:
        page = MagicMock()
        locator = MagicMock()
        page.locator.return_value.first = locator
        profile = {
            "address_line1": "123 Main Street",
            "current_company": "Carleton University (student)",
        }
        locator.evaluate.return_value = "Street Address:*"
        self.assertEqual(
            _grounded_fill_value(page, "r1", "invented", profile)[0],
            "123 Main Street",
        )
        locator.evaluate.return_value = "Current company"
        self.assertEqual(
            _grounded_fill_value(page, "r2", "invented", profile)[0],
            "Carleton University (student)",
        )

    def test_ai_tool_experience_rejects_unsupported_product_and_frequency(self) -> None:
        page = MagicMock()
        locator = MagicMock()
        page.locator.return_value.first = locator
        locator.evaluate.return_value = "Please tell us about your experience using AI tools"
        with self.assertRaisesRegex(ValueError, "unsupported .*claims"):
            _grounded_fill_value(
                page,
                "r1",
                "I use Claude Code daily.",
                {"first_name": "Jane"},
                "Built resume tooling with the Claude API.",
            )

    def test_ai_tool_experience_allows_resume_grounded_product(self) -> None:
        page = MagicMock()
        locator = MagicMock()
        page.locator.return_value.first = locator
        locator.evaluate.return_value = "Please tell us about your experience using AI tools"
        value = "I built resume tooling with the Claude API."
        self.assertEqual(
            _grounded_fill_value(
                page, "r1", value, {"first_name": "Jane"},
                "Built resume tooling with the Claude API.",
            )[0],
            value,
        )

    def test_self_taught_history_requires_explicit_evidence(self) -> None:
        page = MagicMock()
        locator = MagicMock()
        page.locator.return_value.first = locator
        locator.evaluate.return_value = (
            "What is the last technical topic you taught yourself on your own time?"
        )
        with self.assertRaisesRegex(ValueError, "self-directed learning history"):
            _grounded_fill_value(
                page, "r1", "I taught myself Go last summer.",
                {"first_name": "Jane"}, "Built webhook handlers in Go.",
            )

    def test_learning_sources_require_structured_profile(self) -> None:
        page = MagicMock()
        locator = MagicMock()
        page.locator.return_value.first = locator
        locator.evaluate.return_value = "How do you stay current with AI advancements?"
        with self.assertRaisesRegex(ValueError, "learning sources"):
            _grounded_fill_value(
                page, "r1", "I follow several technical blogs.",
                {"first_name": "Jane"}, "Built an ML service.",
            )

    def test_live_production_story_requires_resume_evidence(self) -> None:
        page = MagicMock()
        locator = MagicMock()
        page.locator.return_value.first = locator
        locator.evaluate.return_value = (
            "Give an example of a live production issue with a client."
        )
        with self.assertRaisesRegex(ValueError, "production/client incident history"):
            _grounded_fill_value(
                page, "r1", "I was on call when a client system failed.",
                {"first_name": "Jane"}, "Optimized a data pipeline.",
            )

    def test_plain_text_sponsorship_answer_is_runner_grounded(self) -> None:
        page = MagicMock()
        locator = MagicMock()
        locator.evaluate.return_value = (
            "Do you now or will you in the future require employer "
            "sponsorship to work in the United States?"
        )
        page.locator.return_value.first = locator
        value, note = _grounded_fill_value(page, "r21", "No", {
            "work_authorization": {"US": {
                "authorized": False,
                "requires_sponsorship": True,
            }}
        })
        self.assertEqual(value, "Yes")
        self.assertIn("grounded US sponsorship", note or "")

    def test_plain_text_sponsorship_uses_us_state_job_location(self) -> None:
        page = MagicMock()
        locator = MagicMock()
        locator.evaluate.return_value = (
            "Will you require any Visa sponsorship now or in the future?"
        )
        page.locator.return_value.first = locator
        value, note = _grounded_fill_value(
            page,
            "r21",
            "No",
            {"work_authorization": {"US": {
                "authorized": False,
                "requires_sponsorship": True,
            }}},
            job_locations=["Chicago, IL"],
        )

        self.assertEqual(value, "Yes")
        self.assertIn("grounded US sponsorship", note or "")

    @patch("applyd.apply.tools.pick_option", return_value="ok: picked o1")
    @patch("applyd.apply.tools._read_options")
    @patch("applyd.apply.tools.open_dropdown", return_value="opened r44")
    @patch("applyd.apply.tools._ref_locator")
    def test_referral_dropdown_uses_configured_fallback(
        self, ref_locator, _open, read_options, pick_option_mock
    ) -> None:
        locator = MagicMock()
        locator.get_attribute.side_effect = lambda name: {
            "data-applyd-label": "How did you hear about us?",
            "data-applyd-combobox-open": None,
            "role": "combobox",
        }.get(name)
        ref_locator.return_value = locator
        read_options.return_value = [
            {"ref": "o0", "text": "LinkedIn"},
            {"ref": "o1", "text": "Indeed"},
        ]
        page = MagicMock()
        result = select_option(
            page, "r44", "Company careers page",
            profile={"application_policy": {
                "required_referral_source_fallbacks": [
                    "Company careers page", "LinkedIn", "Indeed", "Other",
                ]
            }},
        )
        self.assertIn("selected 'LinkedIn'", result)
        pick_option_mock.assert_called_once()
        self.assertEqual(pick_option_mock.call_args.args[:2], (page, "o0"))

    def test_uk_radio_cannot_claim_a_temporary_work_visa(self) -> None:
        page = MagicMock()
        locator = MagicMock()
        page.locator.return_value.first = locator
        locator.get_attribute.side_effect = lambda name: {
            "data-applyd-question": "Please confirm your right to work status",
            "data-applyd-option": (
                "I hold another type of visa that gives me the temporary right "
                "to work in the UK and I will require company visa sponsorship"
            ),
            "data-applyd-label": (
                "Please confirm your right to work status — I hold another type "
                "of visa that gives me the temporary right to work in the UK"
            ),
        }.get(name)
        profile = {"work_authorization": {"UK": {
            "authorized": False,
            "requires_sponsorship": True,
        }}}
        result = _profile_click_guard(page, "r12", profile)
        self.assertIn("refused unsupported UK work authorization claim", result or "")

        locator.get_attribute.side_effect = lambda name: {
            "data-applyd-question": "Please confirm your right to work status",
            "data-applyd-option": (
                "I currently do not have the right to work in the UK and would "
                "require company visa sponsorship"
            ),
            "data-applyd-label": (
                "Please confirm your right to work status — I currently do not "
                "have the right to work in the UK and require sponsorship"
            ),
        }.get(name)
        self.assertIsNone(_profile_click_guard(page, "r13", profile))

    def test_known_uk_authorization_is_answerable(self) -> None:
        profile = {"work_authorization": {
            "UK": {
                "authorized": False,
                "requires_sponsorship": True,
                "work_permit": False,
            }
        }}
        self.assertTrue(_profile_already_answers(
            "Are you legally permitted to work in the UK?", profile
        ))
        self.assertTrue(_profile_already_answers(
            "Will you require sponsorship in the United Kingdom?", profile
        ))
        self.assertTrue(_profile_already_answers(
            "Please confirm your right to work status",
            profile,
            job_locations=["London, United Kingdom"],
        ))

    def test_maven_recruitment_event_answer_is_grounded(self) -> None:
        profile = {
            "background_defaults": {
                "recruitment_events": {"Maven Securities": []},
            },
            "application_policy": {
                "ordinary_accuracy_attestation": "authorized",
            },
        }
        self.assertTrue(_profile_already_answers(
            "Have you attended a Maven recruitment event this year?",
            profile,
            company="Maven Securities",
        ))
        self.assertTrue(_profile_already_answers(
            "Please select which event you attended or select N/A",
            profile,
            company="Maven Securities",
        ))
        self.assertTrue(_profile_already_answers(
            "Recruitment Privacy Notice",
            profile,
            company="Maven Securities",
        ))

        page = MagicMock()
        locator = MagicMock()
        page.locator.return_value.first = locator
        locator.get_attribute.side_effect = lambda name: {
            "data-applyd-label": (
                "Have you attended a Maven recruitment event this year?"
            ),
            "data-applyd-question": (
                "Have you attended a Maven recruitment event this year?"
            ),
        }.get(name)
        self.assertIsNone(_select_profile_guard(
            page, "r32", "No", profile, "", [], "Maven Securities"
        ))
        self.assertIn("refused recruitment-event answer", _select_profile_guard(
            page, "r32", "Yes", profile, "", [], "Maven Securities"
        ) or "")

    @patch("applyd.apply.tools._ref_locator")
    def test_raw_event_pick_cannot_invent_attendance(self, ref_locator) -> None:
        locator = MagicMock()
        locator.evaluate.return_value = "div"
        locator.inner_text.return_value = "Yes"
        ref_locator.return_value = locator
        page = MagicMock()
        page.evaluate.return_value = (
            "Have you attended a Maven recruitment event this year?"
        )
        profile = {"background_defaults": {
            "recruitment_events": {"Maven Securities": []},
        }}
        result = pick_option(
            page, "o0", profile, company="Maven Securities"
        )
        self.assertIn("refused recruitment-event answer", result)
        locator.click.assert_not_called()

    def test_unsupported_excel_yes_is_blocked(self) -> None:
        page = MagicMock()
        locator = MagicMock()
        page.locator.return_value.first = locator
        locator.get_attribute.return_value = (
            "Do you have hands-on experience working with Excel?"
        )
        result = _select_profile_guard(
            page, "r30", "Yes", {"work_authorization": {}}, "Python and SQL", []
        )
        self.assertIn("refused unsupported Yes", result or "")

    def test_brightdata_solver_uses_custom_cdp_command(self) -> None:
        page = MagicMock()
        session = page.context.new_cdp_session.return_value
        session.send.return_value = {"status": "solve_finished"}
        status, _detail = _solve_brightdata_captcha(page)
        self.assertEqual(status, "solve_finished")
        page.context.new_cdp_session.assert_called_once_with(page)
        session.send.assert_called_once_with(
            "Captcha.solve", {"detectTimeout": 30_000}
        )

    def test_navigation_uses_runner_bound_url(self) -> None:
        page = object()
        with patch("applyd.apply.tools.navigate", return_value="ok") as navigate:
            result = dispatch(
                page, "navigate", {"url": "https://evil.example"},
                test_mode=True, job_url="https://jobs.example/real",
            )
        self.assertEqual(result, "ok")
        navigate.assert_called_once_with(page, "https://jobs.example/real")

    def test_upload_uses_runner_bound_resume(self) -> None:
        page = object()
        with patch(
            "applyd.apply.tools._resume_upload_guard", return_value=None
        ), patch("applyd.apply.tools.upload_file", return_value="ok") as upload:
            result = dispatch(
                page, "upload_resume", {"ref": "r4", "path": "/tmp/wrong"},
                test_mode=True, resume_pdf_path="/safe/resume.pdf",
            )
        self.assertEqual(result, "ok")
        upload.assert_called_once_with(page, "r4", "/safe/resume.pdf")

    def test_resume_upload_rejects_transcript_field(self) -> None:
        page = MagicMock()
        locator = MagicMock()
        page.locator.return_value.first = locator
        locator.get_attribute.return_value = "Transcript"
        result = _resume_upload_guard(page, "r18")
        self.assertIn("refused non-resume upload target", result or "")

    def test_resume_upload_accepts_explicit_resume_field(self) -> None:
        page = MagicMock()
        locator = MagicMock()
        page.locator.return_value.first = locator
        locator.get_attribute.return_value = "Resume/CV"
        self.assertIsNone(_resume_upload_guard(page, "r7"))

    def test_resume_upload_accepts_greenhouse_attach_with_resume_name(self) -> None:
        page = MagicMock()
        locator = MagicMock()
        page.locator.return_value.first = locator
        locator.get_attribute.return_value = "Attach"
        locator.evaluate.return_value = {
            "id": "resume", "name": "resume", "aria": "",
            "direct": "Attach", "context": "Resume Attach",
        }
        self.assertIsNone(_resume_upload_guard(page, "r11"))

    def test_failed_submit_returns_fresh_snapshot(self) -> None:
        page = _WaitPage()
        with patch("applyd.apply.tools.submit", return_value="error: validation") as do_submit, patch(
            "applyd.apply.tools.snapshot", return_value="r2: [button/submit] 'Submit'"
        ) as do_snapshot:
            result = dispatch(
                page, "submit", {"ref": "r9"}, test_mode=False,
            )
        do_submit.assert_called_once_with(page, "r9", False, verify_ctx=None)
        do_snapshot.assert_called_once_with(page)
        self.assertIn("use only these new refs", result)
        self.assertIn("r2", result)

    def test_submit_rejects_non_submit_ref_even_in_test_mode(self) -> None:
        result = submit(
            _Page({"tag": "a", "type": "", "text": "privacy", "inForm": False}),
            "r9",
            test_mode=True,
        )
        self.assertTrue(result.startswith("error:"))

    def test_submit_accepts_real_form_button_in_test_mode(self) -> None:
        result = submit(
            _Page({"tag": "button", "type": "submit", "text": "submit", "inForm": True}),
            "r9",
            test_mode=True,
        )
        self.assertEqual(result, "ok: test_mode=true; would have clicked r9")

    def test_submit_rejects_non_submit_button_with_submit_html_type(self) -> None:
        result = submit(
            _Page({"tag": "button", "type": "submit", "text": "+ add education", "inForm": True}),
            "r9",
            test_mode=True,
        )
        self.assertTrue(result.startswith("error:"))

    def test_submit_accepts_semantic_button_without_html_form(self) -> None:
        result = submit(
            _Page({"tag": "button", "type": "submit", "text": "submit application", "inForm": False}),
            "r9",
            test_mode=True,
        )
        self.assertEqual(result, "ok: test_mode=true; would have clicked r9")

    def test_generic_click_rejects_submit_controls(self) -> None:
        page = object()
        with patch("applyd.apply.tools._is_submit_control", return_value=True), patch(
            "applyd.apply.tools._click_with_overlay_fallback"
        ) as raw_click:
            result = click(page, "r9")
        self.assertTrue(result.startswith("error:"))
        raw_click.assert_not_called()


if __name__ == "__main__":
    unittest.main()
