"""Offline dashboard browser check with fictional data; no application actions.

Run: PYTHONPATH=src .venv/bin/python scripts/check_dashboard_ui.py
Screenshots are written to ignored out/dashboard-preview/.
"""
from __future__ import annotations

import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import Error, sync_playwright

from applyd.dashboard.server import DashboardData, DashboardServer
from applyd.local_store import LocalStore
from applyd.models import Job


def main():
    output = Path("out/dashboard-preview")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        store = LocalStore(root / "applyd.sqlite3")
        pdf = root / "resume.pdf"
        pdf.write_bytes(b"%PDF-1.4\nfictional demo resume")
        tex = root / "resume.tex"
        tex.write_text("Fictional demo")
        companies = ["Orbit Labs", "Northstar", "Arc Robotics", "Meridian", "Forma", "Stratos", "Evergreen", "Linear Space", "Parallax", "Runway Systems", "Kite", "Cedar AI", "Fable", "Atlas", "Current", "Parallel", "Beacon", "Fern"]
        titles = ["Software Engineer, Platform", "Machine Learning Engineer", "Robotics Software Engineer", "Full Stack Engineer", "Software Engineer, New Grad", "AI Research Engineer"]
        now = datetime.now(timezone.utc)
        for i, company in enumerate(companies):
            job = Job(id=f"demo-{i}", source="ashby", external_id=str(i), company=company,
                      title=titles[i % len(titles)], url="https://example.com/careers",
                      locations=[["San Francisco, CA"], ["Toronto, Canada"], ["Remote"], ["New York, NY"]][i % 4],
                      first_seen_at=now, last_seen_at=now)
            store.upsert([job])
            store.save_tailored_resume(job_id=job.id, source_resume_hash="demo", model="demo", edit_plan={}, latex_path=str(tex), pdf_path=str(pdf), cost_usd=0)
            _, attempt = store.start_apply_attempt(job.id, "demo")
            store.finish_apply_attempt(attempt, status="applied" if i < 14 else "review", reason="Submission confirmed." if i < 14 else "A required question needs your input.")
            with store.transaction() as conn:
                stamp = (now - timedelta(days=i // 2, minutes=i * 5)).isoformat()
                conn.execute("UPDATE apply_attempts SET started_at=?, finished_at=? WHERE id=?", (stamp, stamp, attempt))
        server = DashboardServer(("127.0.0.1", 0), DashboardData(store.path, root))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with sync_playwright() as p:
                try:
                    browser = p.chromium.launch(channel="chrome", headless=True)
                except Error:
                    browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width":1440,"height":1060}, device_scale_factor=1)
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}")
                page.wait_for_function("document.querySelector('#confirmed-count').textContent === '14'")
                assert page.locator("tbody tr").count() == 12
                page.screenshot(path=str(output / "desktop.png"), full_page=True, animations="disabled")
                page.get_by_role("button", name="Next page", exact=True).click()
                assert page.locator("tbody tr").count() == 2
                page.locator("#search").fill("Orbit")
                assert page.locator("tbody tr").count() == 1
                page.locator(".role-button").click()
                assert page.locator("dialog").is_visible()
                assert page.get_by_role("link", name="Open resume", exact=True).get_attribute("href").endswith("/resume")
                page.screenshot(path=str(output / "detail.png"), full_page=True, animations="disabled")
                page.keyboard.press("Escape")
                assert not page.locator("dialog").is_visible()
                page.locator("#search").fill("zzzz")
                assert page.get_by_text("No matching applications", exact=True).is_visible()
                page.get_by_role("button", name="Clear filters", exact=True).click()
                page.locator('[data-view="review"]').click()
                assert page.locator("tbody tr").count() == 4
                page.locator('[data-view="applied"]').click()
                page.set_viewport_size({"width":390,"height":844})
                page.screenshot(path=str(output / "mobile.png"), full_page=True, animations="disabled")
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                page.locator(".role-button").first.click()
                assert page.get_by_role("link", name="Download", exact=True).is_visible()
                page.keyboard.press("Escape")
                empty_payload = {"applications": [], "activity": [{"date": now.date().isoformat(), "count": 0}], "database_exists": False, "refreshed_at": now.isoformat()}
                page.route("**/api/applications", lambda route: route.fulfill(json=empty_payload))
                page.locator("#refresh").click()
                page.get_by_text("Set up your local workspace", exact=True).wait_for()
                assert not errors, errors
                browser.close()
            print("Dashboard browser checks passed: search, pagination, filters, details, PDF links, mobile, empty state.")
            print(f"Screenshots: {output.resolve()}")
        finally:
            server.shutdown()
            server.server_close()
            worker.join()
            store.close()


if __name__ == "__main__":
    main()
