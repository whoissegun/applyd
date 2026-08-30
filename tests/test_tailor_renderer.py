from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from applyd.tailor.structured import TailorPlanError, escape_latex, render_latex


RESUME = {
    "contact": {"name": "A & B", "email": "a_b@example.com"},
    "education": [{
        "institution": "University", "location": "Toronto, ON",
        "degree": "B.C.S.", "dates": "2023--2027",
    }],
    "experience": [{
        "id": "exp1", "company": "Shopify", "title": "Engineer",
        "location": "Toronto", "dates": "2025",
        "bullets": [{"id": "exp1-b1", "text": "Improved throughput by 20%."}],
    }],
    "projects": [{
        "id": "p1", "name": "Applyd", "dates": "2026", "technologies": ["Python"],
        "bullets": [{"id": "p1-b1", "text": "Built a local job agent."}],
    }],
    "skills": {"Languages": ["Python"]},
    "tailoring_policy": {
        "required_experience_ids": ["exp1"],
        "preserve_experience_order": True,
    },
}


def plan() -> dict:
    return {
        "experience_order": ["exp1"],
        "experiences": [{"id": "exp1", "bullets": [{
            "source_ids": ["exp1-b1"],
            "segments": [
                {"text": "Improved throughput by ", "style": "plain"},
                {"text": "20%", "style": "bold"},
                {"text": ".", "style": "plain"},
            ],
        }]}],
        "project_order": ["p1"],
        "projects": [{"id": "p1", "bullets": [{
            "source_ids": ["p1-b1"],
            "segments": [{"text": "Built a local job agent.", "style": "plain"}],
        }]}],
        "skills": {"Languages": ["Python"]},
    }


class TailorRendererTests(unittest.TestCase):
    def test_escape_and_styled_render(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            template = Path(temp) / "template.tex"
            template.write_text(
                "\n".join([
                    "%%APPLYD_HEADER%%", "%%APPLYD_EDUCATION%%",
                    "%%APPLYD_EXPERIENCE%%", "%%APPLYD_PROJECTS%%",
                    "%%APPLYD_SKILLS%%",
                ]),
                encoding="utf-8",
            )
            latex, errors = render_latex(RESUME, plan(), template)
        self.assertEqual(errors, [])
        self.assertIn(r"A \& B", latex)
        self.assertIn(r"\textbf{20\%}", latex)
        self.assertEqual(escape_latex("x_y%"), r"x\_y\%")

    def test_rejects_unknown_source_id(self) -> None:
        invalid = plan()
        invalid["experiences"][0]["bullets"][0]["source_ids"] = ["invented"]
        with tempfile.TemporaryDirectory() as temp:
            template = Path(temp) / "template.tex"
            template.write_text(
                "%%APPLYD_HEADER%%\n%%APPLYD_EDUCATION%%\n%%APPLYD_EXPERIENCE%%\n"
                "%%APPLYD_PROJECTS%%\n%%APPLYD_SKILLS%%",
                encoding="utf-8",
            )
            with self.assertRaises(TailorPlanError):
                render_latex(RESUME, invalid, template)

    def test_reports_missing_required_experience(self) -> None:
        invalid = plan()
        invalid["experience_order"] = []
        with tempfile.TemporaryDirectory() as temp:
            template = Path(temp) / "template.tex"
            template.write_text(
                "%%APPLYD_HEADER%%\n%%APPLYD_EDUCATION%%\n%%APPLYD_EXPERIENCE%%\n"
                "%%APPLYD_PROJECTS%%\n%%APPLYD_SKILLS%%",
                encoding="utf-8",
            )
            _, errors = render_latex(RESUME, invalid, template)
        self.assertTrue(any("missing required" in error for error in errors))

    def test_reports_reordered_experiences(self) -> None:
        resume = dict(RESUME)
        resume["experience"] = [
            RESUME["experience"][0],
            {
                "id": "exp2", "company": "Lyft", "title": "Engineer",
                "location": "Toronto", "dates": "2026",
                "bullets": [{"id": "exp2-b1", "text": "Built a service."}],
            },
        ]
        invalid = plan()
        invalid["experience_order"] = ["exp2", "exp1"]
        invalid["experiences"].append({
            "id": "exp2",
            "bullets": [{
                "source_ids": ["exp2-b1"],
                "segments": [{"text": "Built a service.", "style": "plain"}],
            }],
        })
        with tempfile.TemporaryDirectory() as temp:
            template = Path(temp) / "template.tex"
            template.write_text(
                "%%APPLYD_HEADER%%\n%%APPLYD_EDUCATION%%\n%%APPLYD_EXPERIENCE%%\n"
                "%%APPLYD_PROJECTS%%\n%%APPLYD_SKILLS%%",
                encoding="utf-8",
            )
            _, errors = render_latex(resume, invalid, template)
        self.assertTrue(any("preserve source order" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
