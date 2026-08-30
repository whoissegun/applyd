from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from applyd.apply.tools import (
    TOOL_DEFS,
    _grounded_fill_value,
    _profile_click_guard,
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
    _sanitize_tool_args,
    _sanitize_tool_result,
    _tool_signature,
    _normalize_report_status,
    _profile_already_answers,
    _terminal_tool_verdict,
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
            "Do you currently have the legal right to work in one of these locations?",
            profile,
            job_locations=["Toronto, Canada", "New York, NY"],
        ))

    def test_structured_profile_overrides_false_city_gap(self) -> None:
        self.assertTrue(_profile_already_answers(
            "Location (City)*", {"address_city": "Ottawa"}
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

    @patch("applyd.apply.tools.pick_option", return_value="ok: picked o1")
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
        ref_locator.assert_not_called()
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
        locator.fill.assert_called_once_with("Carleton University", timeout=8000)
        self.assertEqual(read_options.call_count, 2)
        pick_option_mock.assert_called_once()

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
        with patch("applyd.apply.tools.upload_file", return_value="ok") as upload:
            result = dispatch(
                page, "upload_resume", {"ref": "r4", "path": "/tmp/wrong"},
                test_mode=True, resume_pdf_path="/safe/resume.pdf",
            )
        self.assertEqual(result, "ok")
        upload.assert_called_once_with(page, "r4", "/safe/resume.pdf")

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
