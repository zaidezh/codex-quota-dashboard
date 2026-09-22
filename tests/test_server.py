from __future__ import annotations

import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from codex_quota_dashboard.demo import build_demo_snapshot
from codex_quota_dashboard.server import create_server
from codex_quota_dashboard.source import DemoSource, project_runtime, project_snapshot, validate_upstream_url


class ProjectionTests(unittest.TestCase):
    def test_projection_redacts_titles_and_identifiers_without_mutating_input(self) -> None:
        snapshot = build_demo_snapshot()
        snapshot["task_forecast"]["tasks"][0]["session_id"] = "private-session"
        snapshot["task_forecast"]["thread_distribution"]["unknown"] = [{"thread": "private-thread"}]
        snapshot["task_forecast"]["thread_distribution"]["basis"] = [{"turn_id": "private-turn"}]
        original_title = snapshot["task_forecast"]["tasks"][0]["title"]

        projected = project_snapshot(snapshot)

        self.assertEqual(projected["task_forecast"]["tasks"][0]["title"], "本机任务 1")
        self.assertNotIn("session_id", projected["task_forecast"]["tasks"][0])
        self.assertEqual(snapshot["task_forecast"]["tasks"][0]["title"], original_title)
        self.assertEqual(projected["task_forecast"]["scheduled"]["jobs"][0]["name"], "定时任务 1")
        self.assertTrue(projected["task_forecast"]["thread_distribution"]["points"])
        self.assertNotIn("unknown", projected["task_forecast"]["thread_distribution"])
        self.assertNotIn("basis", projected["task_forecast"]["thread_distribution"])

    def test_projection_can_show_titles_only_when_explicit(self) -> None:
        snapshot = build_demo_snapshot()
        projected = project_snapshot(snapshot, show_local_titles=True)
        self.assertEqual(projected["task_forecast"]["tasks"][0]["title"], "示例任务 A")

    def test_upstream_is_loopback_only_by_default(self) -> None:
        self.assertEqual(validate_upstream_url("http://127.0.0.1:18765"), "http://127.0.0.1:18765/")
        with self.assertRaises(ValueError):
            validate_upstream_url("https://example.com")
        self.assertEqual(
            validate_upstream_url("https://example.com/base", allow_remote=True),
            "https://example.com/base/",
        )

    def test_demo_quote_returns_estimate_and_bounds(self) -> None:
        result = DemoSource().quote(
            {
                "items": [
                    {
                        "model": "example-balanced",
                        "calls": 100,
                        "uncached_input": 2500,
                        "cached_input": 7000,
                        "output": 900,
                    }
                ]
            }
        )
        self.assertGreater(result["estimate_pp"], 0)
        self.assertLessEqual(result["lower_pp"], result["estimate_pp"])
        self.assertGreaterEqual(result["upper_pp"], result["estimate_pp"])

    def test_demo_history_and_runtime_match_quota_frontend_contract(self) -> None:
        source = DemoSource()
        history = source.history({})
        runtime = source.runtime({})
        self.assertEqual(history["status"], "ok")
        self.assertTrue(history["composition"]["points"])
        self.assertTrue(history["start"])
        self.assertTrue(history["end"])
        self.assertEqual(runtime["count"], len(runtime["threads"]))

    def test_runtime_projection_redacts_thread_title_and_host(self) -> None:
        projected = project_runtime(
            {
                "status": "ok",
                "threads": [
                    {
                        "thread": "private-thread",
                        "turn": "private-turn",
                        "title": "private title",
                        "host": "private-host",
                        "model": "gpt-example",
                        "effort": "high",
                    }
                ],
            }
        )
        self.assertEqual(projected["threads"][0]["thread"], "local-task-1")
        self.assertEqual(projected["threads"][0]["title"], "本机任务 1")
        self.assertEqual(projected["threads"][0]["host"], "本机")
        self.assertNotIn("turn", projected["threads"][0])


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = create_server("127.0.0.1", 0, DemoSource())
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def get_json(self, path: str) -> dict:
        with urlopen(self.base + path, timeout=3) as response:
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            return json.load(response)

    def test_health_and_dashboard(self) -> None:
        self.assertEqual(self.get_json("/healthz")["mode"], "demo")
        dashboard = self.get_json("/api/dashboard")
        self.assertEqual(dashboard["status"], "ok")
        self.assertEqual(dashboard["privacy"], "local_titles_redacted")
        self.assertIn("adaptive", dashboard["snapshot"])

    def test_static_security_headers(self) -> None:
        with urlopen(self.base + "/", timeout=3) as response:
            body = response.read().decode("utf-8")
            self.assertIn("Codex 额度走势", body)
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

    def test_quote_endpoint(self) -> None:
        body = json.dumps(
            {
                "items": [
                    {
                        "model": "example-fast",
                        "calls": 12,
                        "uncached_input": 1000,
                        "cached_input": 2000,
                        "output": 500,
                    }
                ]
            }
        ).encode("utf-8")
        request = Request(
            self.base + "/api/calibration/quote",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=3) as response:
            result = json.load(response)
        self.assertEqual(result["status"], "ok")

    def test_history_and_runtime_endpoints(self) -> None:
        history = self.get_json("/api/quota/history")
        runtime = self.get_json("/api/quota/runtime")
        self.assertTrue(history["composition"]["points"])
        self.assertEqual(runtime["count"], len(runtime["threads"]))

    def test_unknown_paths_do_not_list_files(self) -> None:
        with self.assertRaises(HTTPError) as context:
            urlopen(self.base + "/vendor/", timeout=3)
        self.assertEqual(context.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
