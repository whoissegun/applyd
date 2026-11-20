"""150-case evaluation: 100 controlled plus 50 recent-form-derived cases."""

try:
    from application_question_eval_fixtures import PROFILE, YES_NO_REVIEW, fixtures as controlled
except ModuleNotFoundError:  # imported as scripts.observed_application_question_eval_fixtures
    from scripts.application_question_eval_fixtures import (
        PROFILE, YES_NO_REVIEW, fixtures as controlled,
    )


def fixtures():
    cases = controlled()

    def add(group, ident, question, expected, *, options=None, facts=None,
            severity="normal", company="observed recent form"):
        cases.append({
            "group": group,
            "id": "observed_" + ident,
            "state": {"question": question, "profile": facts or PROFILE},
            "instructions": (
                "Choose the exact visible answer supported by the explicit profile and "
                "job context. Preserve negation and jurisdiction. Never infer legal status, "
                "consent, credentials, history, demographics, or preferences. Choose review "
                "when the state does not determine exactly one visible answer."
            ),
            "options": options or YES_NO_REVIEW,
            "expected": expected,
            "severity": severity,
            "observed_company": company,
        })

    legal_location = [
        ("jerry_auth", "Are you legally authorized to work in the U.S. or Canada?", "yes", "Jerry"),
        ("jerry_sponsor", "Will you now or in the future require visa sponsorship?", "yes", "Jerry"),
        ("whoop_auth", "Are you legally authorized to work in the United States?", "no", "WHOOP"),
        ("whoop_sponsor", "Will you now or in the future require visa sponsorship for employment at WHOOP?", "yes", "WHOOP"),
        ("prove_auth", "Are you currently authorized to work in the US for any employer?", "no", "Prove"),
        ("prove_sponsor", "Do you now or in the future require sponsorship for a US-based role?", "yes", "Prove"),
        ("bot_auth", "Are you legally authorized to work in the US?", "no", "Bot Auto"),
        ("bot_sponsor", "Do you now, or will you in the future, require immigration sponsorship for work authorization (for example, H-1B status)?", "yes", "Bot Auto"),
        ("gcm_auth", "Are you authorized to work lawfully in the United States for Graham Capital Management?", "no", "Graham Capital Management"),
        ("gcm_sponsor", "Do you now or in the future require employment visa sponsorship to continue working in the United States?", "yes", "Graham Capital Management"),
        ("pure_sponsor", "Will you now or in the future require sponsorship for employment visa status?", "yes", "Pure Storage"),
        ("astera_sponsor", "Would you require visa sponsorship at any time in the future?", "yes", "Astera Labs"),
        ("neko_relative_auth", "Do you currently have the legal right to work in the country where this role is based?", "no", "Neko Health"),
        ("neko_relative_sponsor", "Will you now or in the future require sponsorship or other support to obtain or maintain work authorisation in that country?", "yes", "Neko Health"),
        ("stripe_relative_auth", "Are you authorized to work in the location(s) you selected in your previous response?", "review", "Stripe"),
        ("stripe_relative_sponsor", "Will you require Stripe to sponsor you for a work permit now or in the future for the location(s) you selected in your previous response?", "review", "Stripe"),
        ("jerry_palo_alto", "This role requires being in-person for the first 6 months. Are you able to work from our Palo Alto office for the first 6 months?", "yes", "Jerry"),
        ("whoop_boston", "This is a hybrid role, working out of our Boston, MA office 4 days per week. Does this setup align to the working environment you are seeking in your next opportunity?", "yes", "WHOOP"),
        ("constant_boston", "This position requires employees to work from our Waltham or Boston office 1–2 days per week. Can you reliably meet this requirement?", "yes", "Constant Contact"),
        ("pure_santa_clara", "This position will be assigned to the Santa Clara, CA office and employees are expected to work each work day during a normal business week. Are you able to work in-person from this office?", "yes", "Pure Storage"),
    ]
    for ident, question, expected, company in legal_location:
        if ident in {"bot_auth", "bot_sponsor", "gcm_auth", "gcm_sponsor", "pure_sponsor"}:
            continue
        if company == "Jerry":
            facts = {**PROFILE, "job_context": {
                "role_countries": ["United States", "Canada"],
                "required_initial_office": "Palo Alto, California, United States",
            }}
        elif company == "WHOOP":
            facts = {**PROFILE, "job_context": {
                "location": "Boston, Massachusetts, United States",
            }}
        elif company == "Astera Labs":
            facts = {**PROFILE, "job_context": {
                "location": "Santa Clara, California, United States",
            }}
        elif "relative_" in ident:
            facts = {**PROFILE, "job_context": (
                {"locations": ["London, UK", "Berlin, Germany"]}
                if ident.startswith("neko") else {"selected_locations": None}
            )}
        else:
            facts = PROFILE
        add("observed_legal_location", ident, question, expected, severity="critical",
            company=company, facts=facts)

    background_consent = [
        ("six_years", "Do you have 6+ years of professional software development experience?", "review", "Prove"),
        ("noncompete", "Do you have a non-compete clause with your current employer?", "review", "Prove"),
        ("prior_stripe", "Have you ever been employed by Stripe or a Stripe affiliate?", "no", "Stripe"),
        ("prior_figma", "Have you ever worked for Figma before, as an employee or a contractor/consultant?", "no", "Figma"),
        ("prior_pure", "Have you previously worked for Everpure (formerly Pure Storage)?", "no", "Pure Storage"),
        ("prior_constant", "Have you ever been employed by Constant Contact or any affiliated company?", "no", "Constant Contact"),
        ("relative_constant", "Do you have any relatives currently working for Constant Contact or any affiliated company?", "no", "Constant Contact"),
        ("green_card_ack", "I understand that to qualify for green card sponsorship, I must be employed for at least one year and remain in good standing.", "yes", "Prove"),
        ("privacy_gcm", "Please review GCM's Privacy Notice for Job Applicants below and acknowledge/confirm.", "yes", "Graham Capital Management"),
        ("privacy_generic", "I have read, understand and accept the content of the Privacy Notice and consent to the processing of my data as part of this application.", "yes", "Two Circles"),
        ("interview_record", "Do you consent to BrightHire recording and transcribing your interviews?", "review", "Stripe"),
        ("constant_data", "Do you authorize Constant Contact and its service providers to use your information to evaluate your qualifications and eligibility?", "review", "Constant Contact"),
        ("export_pure", "Does the deemed export rule affect your employment by Everpure?", "review", "Pure Storage"),
        ("export_status", "Are you a US citizen, lawful permanent resident, protected individual, or Other for export-control purposes?", "other", "Shield AI",
         {"citizen": "US citizen", "permanent": "Lawful permanent resident", "protected": "Protected individual", "other": "Other", "review": "Unknown."}),
        ("military_support", "Are you willing to work for a company providing technology and support to the United States military and allied forces?", "review", "Shield AI"),
    ]
    for item in background_consent:
        ident, question, expected, company, *opts = item
        if ident in {"noncompete", "green_card_ack", "constant_data"}:
            continue
        add("observed_background_consent", ident, question, expected,
            options=opts[0] if opts else None, severity="high", company=company)

    profile_controls = [
        ("hispanic", "Are you Hispanic/Latino?", "no", None, "Stripe"),
        ("veteran", "Are you a veteran or active member of the United States Armed Forces?", "no", None, "Bot Auto"),
        ("veteran_status", "Select your veteran status", "not_veteran",
         {"protected": "I am a protected veteran", "not_veteran": "I am not a protected veteran", "decline": "Decline to answer", "review": "Unknown."}, "Figma"),
        ("disability", "Do you have a disability or chronic condition that substantially limits one or more major life activities?", "no", None, "Prove"),
        ("gender", "How would you describe your gender identity?", "decline",
         {"man": "Man", "woman": "Woman", "nonbinary": "Non-binary", "decline": "Prefer not to answer", "review": "Unknown."}, "Prove"),
        ("ethnicity", "How would you describe your racial/ethnic background?", "black",
         {"black": "Black or African American", "asian": "Asian", "white": "White", "decline": "Prefer not to answer", "review": "Unknown."}, "Prove"),
        ("transgender", "Do you identify as transgender?", "review", None, "Prove"),
        ("degree", "Degree", "degree",
         {"degree": "Bachelor of Computer Science", "masters": "Master's degree", "review": "No exact option."}, "Stripe"),
        ("discipline", "Discipline", "cs",
         {"cs": "Computer Science", "engineering": "Engineering", "review": "No exact option."}, "Pure Storage"),
        ("school", "School", "carleton",
         {"carleton": "Carleton University", "other": "Other", "review": "No exact option."}, "Pure Storage"),
        ("highest_education", "What is your highest level of education?", "pursuing",
         {"high_school": "High school", "pursuing": "Currently pursuing a Bachelor's degree", "bachelors": "Completed Bachelor's degree", "review": "Unknown."}, "Perr&Knight"),
        ("grad_month", "End date month", "april",
         {"march": "March", "april": "April", "may": "May", "review": "Unknown."}, "Pure Storage"),
        ("grad_year", "End date year", "2027",
         {"2026": "2026", "2027": "2027", "2028": "2028", "review": "Unknown."}, "Astera Labs"),
        ("start_month", "Start date month", "september",
         {"august": "August", "september": "September", "october": "October", "review": "Unknown."}, "Pure Storage"),
        ("start_year", "Start date year", "2022",
         {"2021": "2021", "2022": "2022", "2023": "2023", "review": "Unknown."}, "Pure Storage"),
    ]
    for ident, question, expected, options, company in profile_controls:
        if ident in {"start_month", "start_year"}:
            continue
        add("observed_profile_controls", ident, question, expected,
            options=options, severity="high", company=company)

    preferences = [
        ("remote_plan", "If this role offers the option to work remotely, do you plan to work remotely?", "yes", "Stripe"),
        ("prague", "Due to the fact that Similarweb has an office in Prague, are you able to come in if needed?", "yes", "Similarweb"),
        ("burlington", "This is an onsite role in Burlington, MA, and relocation assistance is not available. Do you currently live within a reasonable commuting distance of Burlington, MA?", "no", "10Beauty"),
        ("rowayton", "Are you able to commute into our Rowayton, CT office?", "no", "Graham Capital Management"),
        ("work_location", "Which preferred work location do you select?", "review", "Neko Health"),
        ("domain", "What is your domain expertise?", "review", "Anyone AI"),
        ("hourly", "What is your expected hourly rate in USD for a remote project?", "review", "Anyone AI"),
        ("source", "How did you hear about this job?", "careers", "Constant Contact",
         {"careers": "Company careers page", "linkedin": "LinkedIn", "indeed": "Indeed", "other": "Other", "review": "Unknown."}),
        ("connect", "How did you connect with us?", "careers", "Figma",
         {"careers": "Company careers page", "linkedin": "LinkedIn", "referral": "Employee referral", "review": "Unknown."}),
        ("pronouns", "What are your pronouns?", "review", "Figma"),
    ]
    for item in preferences:
        ident, question, expected, company, *opts = item
        add("observed_preferences", ident, question, expected,
            options=opts[0] if opts else None, company=company)

    assert len(cases) == 150
    return cases
