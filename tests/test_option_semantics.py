from __future__ import annotations

import unittest

from applyd.apply import option_semantics as om
from applyd.apply.deterministic_questions import resolve_question
from applyd.apply.tools import _match_option


def opts(*labels):
    return [{"ref": f"o{i}", "text": label} for i, label in enumerate(labels)]


class OptionEquivalenceTests(unittest.TestCase):
    def pick(self, desired, *labels):
        match = _match_option(opts(*labels), desired)
        return match["text"] if match else None

    def test_veteran_wording_variants(self) -> None:
        self.assertEqual("I am not a veteran", self.pick(
            "I am not a protected veteran",
            "Select ...", "I am a veteran", "I am not a veteran", "Decline to self-identify"))

    def test_disability_no_ignores_decline_option(self) -> None:
        self.assertEqual("I do not have a disability", self.pick(
            "No", "I do not have a disability", "I have a disability",
            "I prefer not to share my disability status"))

    def test_sentence_polarity_for_non_legal_questions(self) -> None:
        self.assertEqual("I have never worked for an InterSystems client", self.pick(
            "No", "I currently work for an InterSystems client",
            "I previously worked for an InterSystems client",
            "I have never worked for an InterSystems client"))
        # "Yes" is ambiguous between currently/previously: never guessed.
        self.assertIsNone(self.pick(
            "Yes", "I currently work for an InterSystems client",
            "I previously worked for an InterSystems client",
            "I have never worked for an InterSystems client"))

    def test_legal_sentences_never_use_loose_polarity(self) -> None:
        self.assertIsNone(self.pick(
            "No", "I am authorized to work in the United States for any employer",
            "I require sponsorship to work in the United States",
            "I am not authorized to work in the United States"))

    def test_canadian_province_maps_to_not_in_us(self) -> None:
        self.assertEqual("Not in the U.S.", self.pick(
            "Ontario", "Not in the U.S.", "Alabama", "Alaska", "California"))

    def test_month_formats_match_as_dates(self) -> None:
        self.assertEqual("April 2027", self.pick("2027-04", "December 2026", "April 2027", "May 2027"))
        self.assertEqual("Spring 2027", self.pick("2027-04", "Fall 2026", "Spring 2027", "Summer 2027"))
        # Graduation must be truthful: no option for April 2027 → no match.
        self.assertIsNone(self.pick("2027-04", "Dec 2025", "May 2026", "June 2026", "July 2026"))


class StartDateTests(unittest.TestCase):
    PROFILE = {"expected_grad_date": "2027-04", "earliest_start_date": "immediately"}

    def choose(self, question, *labels):
        options = {str(i): label for i, label in enumerate(labels)}
        answer = resolve_question(question, self.PROFILE, options)
        return options.get(answer.choice)

    def test_picks_nearest_option_on_or_after_graduation(self) -> None:
        self.assertEqual("May 2027", self.choose(
            "Target Start Date*", "January 2027", "May 2027", "September 2027"))
        self.assertEqual("Summer 2027", self.choose(
            "What is your desired start date?", "Fall 2026", "Winter 2027", "Summer 2027", "Fall 2027"))
        self.assertEqual("Spring 2027", self.choose(
            "When can you start?", "Fall 2026", "Spring 2027", "Summer 2027"))

    def test_falls_back_to_nearest_before_when_none_after(self) -> None:
        self.assertEqual("July 2026", self.choose(
            "Target Start Date*", "Dec 2025", "May 2026", "June 2026", "July 2026"))

    def test_parse_month(self) -> None:
        self.assertEqual((2027, (4,)), om.parse_month("2027-04"))
        self.assertEqual((2027, (4,)), om.parse_month("Apr 2027"))
        self.assertEqual((2027, (6, 7, 8)), om.parse_month("Summer 2027"))
        self.assertIsNone(om.parse_month("ASAP"))


class RangeAndRegionTests(unittest.TestCase):
    def pick(self, desired, *labels):
        match = _match_option(opts(*labels), desired)
        return match["text"] if match else None

    def test_graduation_month_inside_ranges(self) -> None:
        self.assertEqual("January 2027 - July 2027", self.pick(
            "2027-04", "I've already graduated", "August 2026 - December 2026",
            "January 2027 - July 2027", "August 2027 - December 2027"))
        self.assertEqual("April to July 2027", self.pick(
            "April 2027", "September to December 2026", "January to March 2027",
            "April to July 2027", "Before September 2026"))
        self.assertEqual("December 2026 - August 2027", self.pick(
            "2027-04", "Prior to December 2026", "December 2026 - August 2027",
            "December 2027 - August 2028"))

    def test_outside_country_option(self) -> None:
        self.assertEqual("Outside of United States and Costa Rica", self.pick(
            "Canada", "United States", "Costa Rica", "Outside of United States and Costa Rica"))
        self.assertIsNone(self.pick("Yes", "United States", "Outside of United States"))

    def test_non_prefixed_option_is_not_equivalent(self) -> None:
        self.assertEqual("Engineering – Computer Science & Technology", self.pick(
            "Computer Science", "Select...", "Business & Management",
            "Engineering (Non-Computer Science)", "Engineering – Computer Science & Technology"))


class GraduationAnchoredStartTests(unittest.TestCase):
    PROFILE = {"expected_grad_date": "2027-04", "earliest_start_date": "immediately"}

    def test_graduation_start_date_is_last_day_of_month(self) -> None:
        from datetime import date
        from applyd.apply.deterministic_questions import graduation_start_date
        self.assertEqual(date(2027, 4, 30), graduation_start_date(self.PROFILE, today=date(2026, 11, 21)))
        self.assertIsNone(graduation_start_date(self.PROFILE, today=date(2027, 6, 1)))

    def test_dropdown_before_graduation_takes_closest(self) -> None:
        # SuperApp: options stop at July 2026 → July 2026 is the closest.
        options = {"0": "Dec 2025", "1": "May 2026", "2": "June 2026", "3": "July 2026"}
        answer = resolve_question("Target Start Date*", self.PROFILE, options)
        self.assertEqual("July 2026", options[answer.choice])

    def test_start_immediately_is_no_while_in_school(self) -> None:
        answer = resolve_question("Can you start immediately?", self.PROFILE,
                                  {"yes": "Yes", "no": "No"})
        self.assertEqual("no", answer.choice)

    def test_typed_date_available_uses_graduation(self) -> None:
        from applyd.apply.deterministic_questions import resolve_text_question
        answer = resolve_text_question("Date available", self.PROFILE, input_type="date")
        self.assertEqual("2027-04-30", answer.value)

    def test_education_start_month_is_not_a_job_start(self) -> None:
        options = {"0": "September 2022", "1": "April 2027"}
        answer = resolve_question("Start date month", self.PROFILE, options)
        self.assertNotEqual("April 2027", options.get(answer.choice))

    def test_graduation_date_stays_exact(self) -> None:
        options = {"0": "Dec 2025", "1": "May 2026", "2": "June 2026", "3": "July 2026"}
        answer = resolve_question("Graduation Date*", self.PROFILE, options)
        self.assertNotIn(answer.choice, options)


class GraduationNearestPolicyTests(unittest.TestCase):
    BASE = {"expected_grad_date": "2027-04", "earliest_start_date": "immediately"}
    ON = dict(BASE, application_policy={"graduation_date_nearest_option": True})
    SUPERAPP = ("Dec 2025", "May 2026", "June 2026", "July 2026")

    def resolve(self, profile, question, labels):
        options = {str(i): label for i, label in enumerate(labels)}
        return options.get(resolve_question(question, profile, options).choice)

    def test_policy_on_picks_closest_graduation_option(self) -> None:
        self.assertEqual("July 2026", self.resolve(self.ON, "Graduation Date*", self.SUPERAPP))
        # Pure closest: March (1 month) beats August (4 months).
        self.assertEqual("March 2027", self.resolve(
            self.ON, "Expected graduation date", ("March 2027", "August 2027")))
        # An exact option still wins over any nearest logic.
        self.assertEqual("April 2027", self.resolve(
            self.ON, "Graduation Date", ("March 2027", "April 2027")))

    def test_policy_off_keeps_graduation_exact(self) -> None:
        self.assertIsNone(self.resolve(self.BASE, "Graduation Date*", self.SUPERAPP))

    def test_guard_allows_only_the_nearest_offered_option(self) -> None:
        from applyd.apply.tools import _date_profile_guard
        self.assertIsNone(_date_profile_guard(
            "Graduation Date*", "July 2026", self.ON, offered=list(self.SUPERAPP)))
        self.assertIsNotNone(_date_profile_guard(
            "Graduation Date*", "May 2026", self.ON, offered=list(self.SUPERAPP)))
        self.assertIsNotNone(_date_profile_guard(
            "Graduation Date*", "July 2026", self.BASE, offered=list(self.SUPERAPP)))

    def test_start_date_guard_accepts_closest_past_option(self) -> None:
        from datetime import date
        from applyd.apply.tools import _date_profile_guard
        self.assertIsNone(_date_profile_guard(
            "Target Start Date*", "July 2026", self.BASE,
            offered=list(self.SUPERAPP), today=date(2026, 11, 21)))
        self.assertIsNotNone(_date_profile_guard(
            "Target Start Date*", "Dec 2025", self.BASE,
            offered=list(self.SUPERAPP), today=date(2026, 11, 21)))


class ComboboxStartAndConsentTests(unittest.TestCase):
    PROFILE = {"expected_grad_date": "2027-04", "earliest_start_date": "immediately"}

    def test_target_start_combobox_gets_graduation_anchor(self) -> None:
        from applyd.apply.deterministic_questions import resolve_text_question
        self.assertEqual("2027-04-30", resolve_text_question("Target Start Date*", self.PROFILE).value)
        self.assertIsNone(resolve_text_question("Start date month", self.PROFILE))
        # The dropdown then binds the closest offered option.
        options = {"0": "June 2026", "1": "July 2026"}
        self.assertEqual("1", resolve_question("Target Start Date*", self.PROFILE, options).choice)

    def test_confirm_deny_consent_options(self) -> None:
        match = _match_option(opts("Confirm", "Deny"), "Yes")
        self.assertEqual("Confirm", match["text"])
        match = _match_option(opts("Confirm", "Deny"), "No")
        self.assertEqual("Deny", match["text"])


class SelectedLocationReaderTests(unittest.TestCase):
    def test_legal_answer_is_not_a_selected_location(self) -> None:
        from applyd.apply import tools
        src = tools._selected_application_locations.__code__.co_consts
        js = next(c for c in src if isinstance(c, str) and "querySelectorAll" in c)
        self.assertIn("authori[sz]|sponsor", js)
        self.assertIn("yes|no|true|false", js)


class NonCompeteTests(unittest.TestCase):
    Q = ("Are you now (or will you be) subject to any agreement containing restrictions "
         "or covenants regarding non-competition or non-solicitation?*")
    OPTIONS = {"yes": "Yes", "no": "No", "review": "Review"}

    def test_consent_policy_never_answers_non_compete(self) -> None:
        profile = {"application_policy": {"required_consents": True}}
        self.assertEqual("review", resolve_question(self.Q, profile, self.OPTIONS, required=True).choice)

    def test_explicit_fact_answers_no(self) -> None:
        profile = {"application_policy": {"required_consents": True},
                   "background_defaults": {"no_restrictive_covenants": True}}
        self.assertEqual("no", resolve_question(self.Q, profile, self.OPTIONS, required=True).choice)

    def test_real_consents_still_yes(self) -> None:
        profile = {"application_policy": {"required_consents": True}}
        for q in ("Do you agree to the following personal data protection regulation?*",
                  "By applying you consent to a background check*"):
            with self.subTest(q=q):
                self.assertEqual("yes", resolve_question(q, profile, self.OPTIONS, required=True).choice)


class SavedAnswerComboboxTests(unittest.TestCase):
    def test_saved_exact_answer_reaches_text_resolver(self) -> None:
        from applyd.apply.deterministic_questions import resolve_text_question
        profile = {"historical_application_answers": {"Area of Interest": "Product Engineering"}}
        self.assertEqual("Product Engineering",
                         resolve_text_question("Area of Interest*", profile).value)
        self.assertIsNone(resolve_text_question("Areas of interest outside work", profile))


class BatchTenFixTests(unittest.TestCase):
    OPTIONS = {"yes": "Yes", "no": "No", "review": "Review"}
    PROFILE = {
        "address_city": "Ottawa", "address_region": "Ontario", "address_country": "Canada",
        "github_url": "https://github.com/example", "portfolio_url": "https://example.dev",
        "disability_status": "No, I do not have a disability",
        "employment_preferences": {"willing_to_work_hybrid": True, "willing_to_work_onsite": True,
                                   "willing_to_relocate": True},
        "application_policy": {"recruiting_communications_consent": False},
        "work_authorization": {"CA": {"authorized": True, "requires_sponsorship": False},
                               "US": {"authorized": False, "requires_sponsorship": True}},
        "background_defaults": {"never_employed_by_target_unless_listed_on_resume": True},
        "resume_employers": ["Lyft", "Shopify"],
    }

    def ask(self, q, **kw):
        return resolve_question(q, self.PROFILE, self.OPTIONS, **kw).choice

    def test_github_or_portfolio_link(self) -> None:
        from applyd.apply.deterministic_questions import resolve_text_question
        self.assertEqual("https://github.com/example", resolve_text_question(
            "Please leave your GitHub or portfolio link if you have one:", self.PROFILE).value)
        self.assertEqual("https://example.dev", resolve_text_question(
            "Link to your portfolio", self.PROFILE).value)

    def test_office_schedule(self) -> None:
        self.assertEqual("yes", self.ask(
            "Are you willing and able to work out of our NYC HQ in Downtown Manhattan 3 times a week?"))
        self.assertEqual("yes", self.ask("Are you able to come into our office a minimum of 3 days a week?"))

    def test_visa_support_is_sponsorship(self) -> None:
        self.assertEqual("no", self.ask("Do you require VISA support?", job_locations=["Ottawa, ON, Canada"]))
        self.assertEqual("yes", self.ask("Do you require VISA support?", job_locations=["NYC"]))

    def test_short_employer_name(self) -> None:
        self.assertEqual("no", self.ask(
            "Are your currently a Built contractor, or have you ever been employed at Built?*",
            company="Built Technologies"))

    def test_accommodations(self) -> None:
        self.assertEqual("no", self.ask(
            "Are there any accommodations we can provide to make your interview process more accessible?"))

    def test_talent_pool_from_options(self) -> None:
        options = {"0": "Yes - you can keep my CV on file for up to 3 years",
                   "1": "No - please delete my data after the process"}
        self.assertEqual("1", resolve_question(
            "Keeping your data safe is really important to us **", self.PROFILE, options).choice)

    def test_prior_application_requires_attestation(self) -> None:
        profile = dict(self.PROFILE, previously_applied_companies=["Fidus Systems"])
        self.assertEqual("review", resolve_question(
            "Have you applied to Grayce before?", profile, self.OPTIONS, company="Grayce").choice)
        profile["application_policy"] = {"application_history_complete": True}
        self.assertEqual("no", resolve_question(
            "Have you applied to Grayce before?", profile, self.OPTIONS, company="Grayce").choice)
