"""History and results must survive job records that have no date."""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config  # noqa: E402
import app as app_module  # noqa: E402


def _stamp_mtime(path, when):
    ts = when.timestamp()
    os.utime(path, (ts, ts))
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


class HistoryMissingDateTests(unittest.TestCase):
    def setUp(self):
        self._saved_jobs = app_module.jobs
        app_module.jobs = {}
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(config, "RESULTS_FOLDER", self._tmp.name)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        app_module.jobs = self._saved_jobs
        self._tmp.cleanup()

    def _write_job_state(self, job_id, state, when=None):
        job_dir = os.path.join(self._tmp.name, job_id)
        os.makedirs(job_dir, exist_ok=True)
        path = os.path.join(job_dir, "job_state.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        if when is not None:
            _stamp_mtime(path, when)
        with open(path, "rb") as handle:
            return path, handle.read()

    def test_history_lists_dateless_results_and_ignores_failed_runs(self):
        reviewed = datetime(2024, 3, 2, 9, 30)
        job_dir = os.path.join(self._tmp.name, "wife01")
        os.makedirs(job_dir)
        analysis_path = os.path.join(job_dir, "analysis.json")
        with open(analysis_path, "w", encoding="utf-8") as handle:
            json.dump({"overall_score": 0.82, "jurisdiction": {"state_abbrev": "LA"}}, handle)
        expected_date = _stamp_mtime(analysis_path, reviewed)

        # Completed review whose saved record never got a date key.
        app_module.jobs["wife01"] = {
            "id": "wife01",
            "client_name": "Wife Client",
            "status": "complete",
            "results": {"overall_score": 0.82, "jurisdiction": {"state_abbrev": "LA"}},
            "jurisdiction": {"state": "Louisiana", "state_abbrev": "LA"},
        }
        # Newer stored date should sort above the mtime fallback.
        app_module.jobs["newer1"] = {
            "id": "newer1",
            "client_name": "Newer Client",
            "status": "complete",
            "date": "2026-10-01 12:00",
            "results": {"overall_score": 0.4},
        }
        # The 401-era failure: job_state only, no date, no analysis. Must not 500
        # and must not appear as a finished review.
        app_module.jobs["fail401"] = {
            "id": "fail401",
            "client_name": "Failed Auth",
            "status": "error",
            "error": "API authentication failed. Please check the Anthropic API key in config.",
        }
        # Usable results with a non-complete status and no date still show.
        app_module.jobs["partial"] = {
            "id": "partial",
            "status": "error",
            "results": {"overall_score": 0.5},
        }

        client = app_module.app.test_client()
        response = client.get("/history")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Wife Client", body)
        self.assertIn(expected_date, body)
        self.assertIn("82%", body)
        self.assertIn("Newer Client", body)
        self.assertIn("Unknown", body)  # partial record has results but no client name
        self.assertNotIn("Failed Auth", body)
        self.assertLess(body.index("Newer Client"), body.index("Wife Client"))

        results = client.get("/results/wife01")
        self.assertEqual(results.status_code, 200)
        self.assertIn(expected_date, results.get_data(as_text=True))

        status = client.get("/status/fail401")
        self.assertEqual(status.status_code, 200)

    def test_recover_dateless_job_state_does_not_rewrite_files(self):
        when = datetime(2024, 1, 15, 8, 0)
        original = {
            "id": "erronly",
            "client_name": "Interrupted",
            "status": "error",
            "progress": 40,
            "status_message": "Error: API authentication failed.",
            "error": "API authentication failed.",
            "has_results": False,
            "primary_docx_filename": None,
            "scorecard_only": False,
        }
        path, before = self._write_job_state("erronly", original, when)
        expected_date = datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M")

        recovered = app_module._recover_job("erronly")

        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.get("date"), expected_date)
        self.assertEqual(recovered.get("client_name"), "Interrupted")
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), before)
        self.assertNotIn("date", json.loads(before.decode("utf-8")))

    def test_recover_keeps_analysis_when_job_state_has_no_date(self):
        job_dir = os.path.join(self._tmp.name, "done01")
        os.makedirs(job_dir)
        analysis = {
            "overall_score": 0.66,
            "jurisdiction": {"state": "Florida", "state_abbrev": "FL"},
        }
        analysis_path = os.path.join(job_dir, "analysis.json")
        with open(analysis_path, "w", encoding="utf-8") as handle:
            json.dump(analysis, handle)
        when = datetime(2025, 6, 1, 14, 45)
        expected_date = _stamp_mtime(analysis_path, when)
        state_path, before = self._write_job_state(
            "done01",
            {
                "id": "done01",
                "client_name": "Saved Review",
                "status": "complete",
                "progress": 100,
                "status_message": "Review complete!",
                "error": None,
                "has_results": True,
                "primary_docx_filename": "contract.docx",
                "scorecard_only": False,
            },
        )

        recovered = app_module._recover_job("done01")

        self.assertEqual(recovered.get("status"), "complete")
        self.assertEqual(recovered.get("client_name"), "Saved Review")
        self.assertEqual(recovered.get("date"), expected_date)
        self.assertEqual(recovered.get("results", {}).get("overall_score"), 0.66)
        self.assertEqual(recovered.get("jurisdiction", {}).get("state_abbrev"), "FL")
        with open(state_path, "rb") as handle:
            self.assertEqual(handle.read(), before)
        with open(analysis_path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["overall_score"], 0.66)

        rows = app_module._history_rows(app_module.jobs)
        self.assertEqual([row["client_name"] for row in rows], ["Saved Review"])
        self.assertEqual(rows[0]["date"], expected_date)
        self.assertEqual(rows[0]["overall_score"], 66)

    def test_persist_includes_date_for_new_jobs(self):
        job_id = "fresh01"
        os.makedirs(os.path.join(self._tmp.name, job_id))
        app_module.jobs[job_id] = {
            "id": job_id,
            "client_name": "New Review",
            "status": "processing",
            "progress": 3,
            "status_message": "Starting analysis...",
            "error": None,
            "results": None,
            "primary_docx_filename": None,
            "scorecard_only": False,
            "date": "2026-10-27 09:15",
        }

        app_module._persist_job(job_id)

        with open(os.path.join(self._tmp.name, job_id, "job_state.json"), encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved.get("date"), "2026-10-27 09:15")


if __name__ == "__main__":
    unittest.main()
