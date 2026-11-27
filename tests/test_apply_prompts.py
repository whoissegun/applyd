from __future__ import annotations

import json
import unittest

from applyd.apply.prompts import (
    MAX_TAILOR_METADATA_CHARS,
    _compact_tailor_metadata,
    _resume_grounding_context,
    build_user_blocks,
)


class ApplyPromptTests(unittest.TestCase):
    def test_narrative_exception_is_explicit_and_optional_answers_are_skipped(self):
        from applyd.apply.prompts import SYSTEM_PROMPT
        self.assertIn('required_narrative_invention', SYSTEM_PROMPT)
        self.assertIn('Optional → always skip', SYSTEM_PROMPT)
        self.assertIn('Core facts remain grounded regardless of control type', SYSTEM_PROMPT)

    def test_resume_context_drops_layout_preamble_but_preserves_body(self) -> None:
        resume = (
            "\\documentclass{article}\n"
            "\\newcommand{\\resumeItem}[1]{#1}\n"
            "\\begin{document}\n"
            "Grounded employer, technology, and metric: Python 20%.\n"
            "\\end{document}\n"
        )

        context = _resume_grounding_context(resume)

        self.assertNotIn("documentclass", context)
        self.assertNotIn("newcommand", context)
        self.assertIn("Grounded employer, technology, and metric: Python 20%.", context)

    def test_tailor_metadata_omits_repeated_rewritten_content(self) -> None:
        metadata = {
            "experience_order": ["exp-1"],
            "experiences": [{"id": "exp-1", "bullets": ["x" * 5_000]}],
            "project_order": ["project-1"],
            "projects": [{"id": "project-1", "bullets": ["y" * 5_000]}],
            "skills": {"languages": ["Python"]},
            "summary": "Emphasize grounded ML systems work.",
            "risk_flags": ["Do not add ungrounded credentials."],
        }

        compact = _compact_tailor_metadata(json.dumps(metadata))
        parsed = json.loads(compact)

        self.assertLessEqual(len(compact), MAX_TAILOR_METADATA_CHARS)
        self.assertEqual(parsed["risk_flags"], metadata["risk_flags"])
        self.assertNotIn("summary", parsed)
        self.assertNotIn("experience_order", parsed)
        self.assertNotIn("project_order", parsed)
        self.assertNotIn("experiences", parsed)
        self.assertNotIn("projects", parsed)
        self.assertNotIn("skills", parsed)

    def test_user_blocks_compact_profile_json_without_changing_values(self) -> None:
        profile = {
            "identity": {"name": "Candidate"},
            "preferences": {"willing_to_relocate": True},
        }
        blocks = build_user_blocks(
            job_id="job-1",
            company="Example",
            title="Engineer",
            job_url="https://example.test/apply",
            resume_pdf_path="/tmp/resume.pdf",
            profile_md=json.dumps(profile, indent=2),
            job_locations=["Ottawa"],
            resume_tex="grounded resume",
            tailor_metadata_json="{}",
            test_mode=True,
        )

        self.assertIn(json.dumps(profile, separators=(",", ":")), blocks[0]["text"])
        self.assertNotIn('\n  "identity"', blocks[0]["text"])


if __name__ == "__main__":
    unittest.main()
