# applyd profile

The agent reads this file as a single prose blob and reasons over it to answer
job-application forms. Write in plain language. Include formatting hints where
forms are picky (phone, dates). Copy this to `profile.md` and fill it in.

---

## Identity

- **Full legal name:** Jane Doe
- **Preferred name:** Jane
- **Email:** jane@example.com
- **Phone:** +1 555-555-5555 (format as `+1-555-555-5555` if the form wants dashes)
- **Pronouns:** she/her (decline to answer if optional)

## Location

Currently based in Ottawa, Ontario, Canada. Country code `CA`.

## Links

- LinkedIn: https://www.linkedin.com/in/jane-doe/
- GitHub: https://github.com/janedoe
- Portfolio: (none)

## Education

- School: Carleton University
- Degree: Bachelor of Computer Science
- Expected graduation: April 2027 (write as `2027-04` if form wants ISO, or
  `April 2027` for free text, or `04/2027` for MM/YYYY).
- GPA: decline to answer unless the form requires it.

## Work authorization

Rules, per country the form asks about:

- **Canada (CA):** authorized to work. Holds a Canadian Work Permit. Does NOT
  require sponsorship.
- **United States (US):** not currently authorized. Requires visa sponsorship
  (typically TN, H-1B, or OPT-equivalent).
- **Other countries (UK, EU, etc.):** not authorized, requires sponsorship.

Answer truthfully even if it disqualifies the application — that's fine.

## Demographics (all optional; decline when allowed)

- Gender: prefer not to say
- Ethnicity / race: Black or African American
- Hispanic/Latino: No
- Veteran status: I am not a protected veteran
- Disability status: I do not wish to answer

If the form forces a choice and none of the options match the preferred answer,
pick the closest neutral option (e.g. "Decline to self-identify") rather than
guessing.

## Job preferences

- Earliest start date: flexible; use the form's default or "ASAP" if required.
- Salary expectation: Negotiable. If the form requires a number, write
  "Negotiable" as a string; if it requires a number, leave blank and skip this job.
- How did you hear about us: "Company careers page" unless a referral is known.

## Agent rules

- **Never invent facts.** If the form asks something not covered here, skip the
  job and notify the user.
- **Custom challenges / puzzles / coding screeners embedded in the form** — skip
  the job, ping the user with the URL so they can handle it manually.
- **CAPTCHAs** — skip the job if it blocks submission.
- **Resume upload** — use the tailored PDF path provided with the task payload.
