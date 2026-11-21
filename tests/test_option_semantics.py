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
