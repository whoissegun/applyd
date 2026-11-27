"""One hundred labeled application-question cases derived from profile policy.

Expected labels are local evaluation data and are never included in model
requests. The state uses generalized facts rather than identity/contact data.
"""


PROFILE = {
    "address": {"city": "Ottawa", "region": "Ontario", "country": "Canada"},
    "work_authorization": {
        "CA": {"authorized": True, "requires_sponsorship": False,
               "status": "valid Canadian study permit and open work permit"},
        "US": {"authorized": False, "requires_sponsorship": True},
        "UK": {"authorized": False, "requires_sponsorship": True},
        "EU": {"authorized": False, "requires_sponsorship": True},
    },
    "education": {"degree": "Bachelor of Computer Science",
                  "institution": "Carleton University", "major": "Computer Science",
                  "start_date": "2022-09", "expected_graduation": "2027-04",
                  "currently_student": True},
    "employment": {"years_professional_experience": None,
                   "willing_relocate": True, "willing_onsite": True,
                   "willing_hybrid": True, "willing_remote": True,
                   "willing_any_onsite_schedule": True,
                   "willing_travel": True,
                   "employment_types": ["contract", "full_time", "part_time"],
                   "earliest_start": "immediately"},
    "policy": {"recruiting_marketing_consent": False,
               "sms_recruiting_consent": False,
               "interview_recording_consent": None,
               "ordinary_accuracy_attestation": True},
    "credentials": {"security_clearance_US": False,
                    "licensed_professional_engineer": False,
                    "drivers_license": None},
    "identity_facts": {"citizenships": ["Nigeria"], "over_18": True,
                       "us_person": False, "us_lawful_permanent_resident": False,
                       "us_protected_individual": False},
    "demographics": {"gender": "Prefer not to say", "ethnicity": "Black or African American",
                     "hispanic_latino": False, "veteran_status": "Not a protected veteran",
                     "disability_status": "No disability"},
    "background": {"never_employed_by_target_unless_listed": True,
                   "no_immediate_family_at_target_company": True,
                   "no_immediate_family_in_government": True},
    "referral_source": "Company careers page",
    "user_policy": {"combined_us_ca_require_authorization_answer": False},
}


YES_NO_REVIEW = {"yes": "Yes", "no": "No", "review": "Missing, ambiguous, or unsupported."}


def fixtures():
    cases = []

    def add(group, ident, question, expected, *, options=None, facts=None, severity="normal"):
        cases.append({
            "group": group,
            "id": ident,
            "state": {"question": question, "profile": facts or PROFILE},
            "instructions": (
                "Choose the exact visible answer supported by the explicit profile. "
                "Preserve negation and jurisdiction. Never infer legal status, consent, "
                "credentials, history, or preferences. Choose review when wording or facts "
                "do not determine exactly one answer."
            ),
            "options": options or YES_NO_REVIEW,
            "expected": expected,
            "severity": severity,
        })

    legal_1 = [
        ("ca_authorized_1", "Are you legally authorized to work in Canada?", "yes"),
        ("ca_authorized_2", "Do you have the legal right to work in Canada?", "yes"),
        ("ca_authorized_3", "Can you work lawfully in Canada?", "yes"),
        ("ca_not_authorized", "Are you not authorized to work in Canada?", "no"),
        ("ca_sponsor_1", "Will you require sponsorship to work in Canada?", "no"),
        ("ca_sponsor_2", "Do you now or in the future require Canadian visa sponsorship?", "no"),
        ("ca_no_sponsor", "Can you work in Canada without employer sponsorship?", "yes"),
        ("ca_negative_sponsor", "Is it true that you will not require sponsorship in Canada?", "yes"),
        ("ca_false_negative", "Is it false that you need Canadian sponsorship?", "yes"),
        ("ca_permit", "Do you currently hold valid authorization to work in Canada?", "yes"),
        ("us_authorized_1", "Are you legally authorized to work in the United States?", "no"),
        ("us_authorized_2", "Do you have unrestricted US work authorization?", "no"),
        ("us_sponsor_1", "Will you require sponsorship to work in the United States?", "yes"),
        ("us_sponsor_2", "Do you now or later need US employment visa sponsorship?", "yes"),
        ("us_without_sponsor", "Can you work in the US without sponsorship?", "no"),
        ("us_negative_sponsor", "Is it true that you will not need sponsorship in the US?", "no"),
        ("us_citizen", "Are you a United States citizen?", "no"),
        ("us_green_card", "Are you a US permanent resident or green-card holder?", "no"),
        ("ca_citizen", "Are you a Canadian citizen?", "no"),
        ("ca_permanent_resident", "Are you a Canadian permanent resident?", "no"),
    ]
    for ident, question, expected in legal_1:
        add("legal_ca_us", ident, question, expected, severity="critical")

    legal_2 = [
        ("uk_authorized", "Are you authorized to work in the United Kingdom?", "no"),
        ("uk_sponsor", "Will you require sponsorship to work in the UK?", "yes"),
        ("uk_without_sponsor", "Can you work in Britain without visa sponsorship?", "no"),
        ("eu_authorized", "Are you authorized to work in the European Union?", "no"),
        ("eu_sponsor", "Would you need sponsorship for employment in the EU?", "yes"),
        ("eu_without_sponsor", "Do you have the right to work anywhere in the EU without sponsorship?", "no"),
        ("both_us_ca", "Are you authorized to work in both the United States and Canada?", "no"),
        ("at_least_one_us_ca", "Are you authorized to work in at least one of the United States or Canada?", "yes"),
        ("either_us_ca_ambiguous", "Are you authorized to work in the United States or Canada?", "review"),
        ("user_exact_combined", "Do you require authorization to work in the U.S. or Canada?", "no"),
        ("sponsor_either_ambiguous", "Will you need sponsorship in the United States or Canada?", "review"),
        ("sponsor_both", "Will you require sponsorship in both the United States and Canada?", "no"),
        ("ca_and_uk_authorized", "Are you authorized to work in Canada and the UK?", "no"),
        ("ca_or_uk_at_least_one", "Are you authorized in at least one of Canada or the UK?", "yes"),
        ("global_authorized", "Are you authorized to work anywhere worldwide?", "review"),
        ("job_country_missing", "Are you authorized to work in the country where this job is located?", "review"),
        ("visa_help", "Will you require immigration assistance?", "review"),
        ("future_status_unspecified", "Could your work authorization change in the future?", "review"),
        ("status_details", "Which immigration status do you hold?", "ca_status",
         {"ca_status": "Canadian study permit and open work permit", "review": "Unknown or unsupported."}),
        ("status_wrong_option", "Select your Canadian work status", "review",
         {"citizen": "Citizen", "permanent": "Permanent resident", "closed": "Closed employer-specific permit", "review": "No exact supported option."}),
    ]
    for item in legal_2:
        ident, question, expected, *rest = item
        add("legal_compound", ident, question, expected,
            options=rest[0] if rest else None, severity="critical")

    location_education = [
        ("live_canada", "Do you currently live in Canada?", "yes"),
        ("live_ottawa", "Are you currently based in Ottawa?", "yes"),
        ("live_toronto", "Do you currently reside in Toronto?", "no"),
        ("live_london", "Do you currently live in London, UK?", "no"),
        ("live_seoul", "Are you currently based in Seoul?", "no"),
        ("relocate", "Are you willing to relocate for this role?", "yes"),
        ("onsite", "Are you willing to work onsite?", "yes"),
        ("hybrid", "Are you willing to work a hybrid schedule?", "yes"),
        ("remote", "Are you willing to work remotely?", "yes"),
        ("onsite_five", "Can you work onsite five days per week?", "yes"),
        ("commute_seoul", "Are you currently able to commute to an office in Seoul?", "no"),
        ("move_seoul", "Would you relocate to Seoul for this position?", "yes"),
        ("student", "Are you currently a student?", "yes"),
        ("new_grad", "Are you a student or new graduate?", "yes"),
        ("graduated", "Have you already graduated?", "no"),
        ("bachelors", "Are you pursuing a bachelor's degree?", "yes"),
        ("masters", "Are you pursuing or have you completed a master's degree?", "no"),
        ("grad_2027", "Do you expect to graduate in 2027?", "yes"),
        ("grad_2026", "Will you graduate in 2026?", "no"),
        ("six_years", "Do you have at least six years of professional software experience?", "review"),
    ]
    for ident, question, expected in location_education:
        add("location_education", ident, question, expected,
            severity="high" if ident in {"live_canada", "live_london", "live_seoul"} else "normal")

    consent_credentials = [
        ("marketing", "May we send you optional recruiting marketing emails?", "no"),
        ("sms", "Do you consent to recruiting text messages?", "no"),
        ("recording", "Do you consent to AI recording of your interview?", "review"),
        ("privacy_ack", "Do you acknowledge the applicant privacy notice?", "yes"),
        ("accuracy", "Do you certify that this application is accurate?", "yes"),
        ("future_roles", "May we contact you about future job opportunities?", "no"),
        ("talent_network", "Would you like to join our talent network?", "no"),
        ("automated_decision", "Do you consent to automated hiring decisions?", "review"),
        ("arbitration", "Do you agree to binding arbitration?", "review"),
        ("background_check", "Do you consent to a background check now?", "review"),
        ("clearance", "Do you currently hold an active US security clearance?", "no"),
        ("clearance_eligible", "Are you eligible to obtain a US security clearance?", "review"),
        ("pe_license", "Are you a licensed Professional Engineer?", "no"),
        ("drivers_license", "Do you have a valid driver's license?", "review"),
        ("criminal_history", "Have you ever been convicted of a criminal offence?", "review"),
        ("government_family", "Do you have an immediate family member in government?", "no"),
        ("prior_company", "Have you previously worked for this employer?", "no"),
        ("noncompete", "Are you bound by a non-compete agreement?", "review"),
        ("over18", "Are you at least 18 years old?", "yes"),
        ("export_control", "Are you a US person under export-control law?", "no"),
    ]
    for ident, question, expected in consent_credentials:
        add("consent_credentials", ident, question, expected,
            severity="critical" if ident in {"recording", "automated_decision", "arbitration"} else "high")

    preferences = [
        # Policy 2026-11-21: still in school, so start anchors on graduation.
        ("start_now", "Are you available to start immediately?", "no"),
        ("start_past", "Could you have started on January 1, 2025?", "review"),
        ("weekends", "Are you willing to work weekends?", "review"),
        ("travel", "Are you willing to travel for work?", "yes"),
        ("contract", "Are you open to contract employment?", "yes"),
        ("fulltime", "Are you interested in full-time employment?", "yes"),
        ("parttime", "Are you open to part-time employment?", "yes"),
        ("salary", "Will you accept a salary of $80,000?", "review"),
        ("interview_python", "Would you prefer to interview in Python?", "review"),
        ("english_fluent", "Are you fluent in English?", "review"),
        ("french_fluent", "Are you fluent in French?", "review"),
        ("referral", "Were you referred by a current employee?", "review"),
        ("source", "Did you hear about this role through LinkedIn?", "review"),
        ("remote_only", "Will you only accept fully remote work?", "no"),
        ("office_any", "Can you work from whichever office the role requires?", "yes"),
        ("relocation_help", "Will you require relocation assistance?", "review"),
        ("shift", "Can you work the overnight shift?", "review"),
        ("notice", "Do you need to give two weeks' notice?", "no"),
        ("name_pronunciation", "Is your name pronounced Dye-vine?", "review"),
        ("vague_help", "Will you need assistance?", "review"),
    ]
    for ident, question, expected in preferences:
        add("preferences_unknowns", ident, question, expected)

    assert len(cases) == 100
    return cases
