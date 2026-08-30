from __future__ import annotations

import unittest

from applyd.resume_import import convert_resume_tex


class ResumeImportTests(unittest.TestCase):
    def test_imports_experience_and_stable_bullet_ids(self) -> None:
        tex = r"""
\section{Experience}
\resumeSubheading{Software Developer Intern}{May 2026 - Aug 2026}{Lyft}{Toronto, ON}
\resumeItemListStart
\resumeItem{Designed idempotent \textbf{Go} handlers, preventing duplicate tips}
\resumeItemListEnd
\section{Projects}
"""
        role = convert_resume_tex(tex, {"full_name": "Jane Example"})["experience"][0]
        self.assertEqual(role["id"], "lyft-software-developer-intern")
        self.assertEqual(role["company"], "Lyft")
        self.assertEqual(role["bullets"], [{
            "id": "lyft-software-developer-intern-1",
            "text": "Designed idempotent Go handlers, preventing duplicate tips",
        }])


if __name__ == "__main__":
    unittest.main()
