import importlib.util
import json
import logging
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path


MODULE_PATH = Path(__file__).parent / "GWS" / "gws-log-collection" / "gws-get-logs.py"


def load_module():
    sys.modules.setdefault("requests", types.SimpleNamespace(get=lambda *args, **kwargs: None))
    googleapiclient = types.ModuleType("googleapiclient")
    discovery = types.ModuleType("googleapiclient.discovery")
    discovery.build = lambda *args, **kwargs: None
    sys.modules.setdefault("googleapiclient", googleapiclient)
    sys.modules.setdefault("googleapiclient.discovery", discovery)

    google = types.ModuleType("google")
    oauth2 = types.ModuleType("google.oauth2")
    service_account = types.ModuleType("google.oauth2.service_account")
    service_account.Credentials = types.SimpleNamespace(from_service_account_file=lambda *args, **kwargs: None)
    sys.modules.setdefault("google", google)
    sys.modules.setdefault("google.oauth2", oauth2)
    sys.modules.setdefault("google.oauth2.service_account", service_account)

    dateutil = types.ModuleType("dateutil")
    parser = types.ModuleType("dateutil.parser")
    tz = types.ModuleType("dateutil.tz")
    parser.parse = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))
    tz.gettz = lambda name: timezone.utc if name == "UTC" else None
    sys.modules.setdefault("dateutil", dateutil)
    sys.modules.setdefault("dateutil.parser", parser)
    sys.modules.setdefault("dateutil.tz", tz)

    spec = importlib.util.spec_from_file_location("gws_get_logs", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def activity(timestamp, name):
    return {"id": {"time": timestamp, "uniqueQualifier": name}}


class FakeListCall:
    def __init__(self, page):
        self.page = page

    def execute(self):
        if isinstance(self.page, Exception):
            raise self.page
        return self.page


class FakeActivities:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        index = len(self.calls) - 1
        return FakeListCall(self.pages[index])


class FakeService:
    def __init__(self, pages):
        self.fake_activities = FakeActivities(pages)

    def activities(self):
        return self.fake_activities


class GoogleWorkspaceLogTests(unittest.TestCase):
    def test_get_activity_logs_follows_all_result_pages(self):
        module = load_module()
        google = module.Google.__new__(module.Google)
        google.service = FakeService(
            [
                {
                    "items": [activity("2026-01-01T00:01:00Z", "second")],
                    "nextPageToken": "page-two",
                },
                {
                    "items": [activity("2026-01-01T00:00:00Z", "first")],
                },
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            output_file = Path(tmp) / "login_logs.json"
            saved, found = google._get_activity_logs("login", str(output_file), overwrite=True)
            written = [json.loads(line) for line in output_file.read_text().splitlines()]

        self.assertEqual(saved, 2)
        self.assertEqual(found, 2)
        self.assertEqual([entry["id"]["uniqueQualifier"] for entry in written], ["first", "second"])
        self.assertEqual(
            google.service.fake_activities.calls,
            [
                {"userKey": "all", "applicationName": "login"},
                {"userKey": "all", "applicationName": "login", "pageToken": "page-two"},
            ],
        )

    def test_get_activity_logs_writes_pages_in_global_chronological_order(self):
        module = load_module()
        google = module.Google.__new__(module.Google)
        google.service = FakeService(
            [
                {
                    "items": [
                        activity("2026-01-01T00:03:00Z", "newest"),
                        activity("2026-01-01T00:02:00Z", "newer"),
                    ],
                    "nextPageToken": "older-page",
                },
                {
                    "items": [
                        activity("2026-01-01T00:01:00Z", "older"),
                        activity("2026-01-01T00:00:00Z", "oldest"),
                    ],
                },
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            output_file = Path(tmp) / "login_logs.json"
            google._get_activity_logs("login", str(output_file), overwrite=True)
            written = [json.loads(line) for line in output_file.read_text().splitlines()]
            temp_files = list(Path(tmp).glob("gws_*.json"))

        self.assertEqual(
            [entry["id"]["uniqueQualifier"] for entry in written],
            ["oldest", "older", "newer", "newest"],
        )
        self.assertEqual(temp_files, [])

    def test_get_activity_logs_does_not_write_partial_page_on_failure(self):
        module = load_module()
        google = module.Google.__new__(module.Google)
        google.service = FakeService(
            [
                {
                    "items": [activity("2026-01-01T00:01:00Z", "newer")],
                    "nextPageToken": "failing-page",
                },
                TypeError("transient API failure"),
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            output_file = Path(tmp) / "login_logs.json"
            with self.assertLogs(level=logging.ERROR):
                saved, found = google._get_activity_logs("login", str(output_file), overwrite=True)

        self.assertEqual((saved, found), (False, False))
        self.assertFalse(output_file.exists())
        self.assertEqual(list(Path(tmp).glob("gws_*.json")), [])

    def test_get_activity_logs_uses_start_time_when_cutoff_is_known(self):
        module = load_module()
        google = module.Google.__new__(module.Google)
        google.service = FakeService(
            [
                {
                    "items": [activity("2026-01-01T00:01:00Z", "newer")],
                },
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            output_file = Path(tmp) / "login_logs.json"
            google._get_activity_logs(
                "login",
                str(output_file),
                overwrite=True,
                only_after_datetime=datetime.fromisoformat("2026-01-01T00:00:00+00:00"),
            )

        self.assertEqual(
            google.service.fake_activities.calls[0],
            {
                "userKey": "all",
                "applicationName": "login",
                "startTime": "2026-01-01T00:00:00Z",
            },
        )

    def test_update_mode_uses_each_apps_own_recent_timestamp(self):
        module = load_module()
        google = module.Google.__new__(module.Google)

        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp)
            (output_path / "login_logs.json").write_text(
                json.dumps(activity("2026-01-02T00:00:00Z", "login-existing")) + "\n"
            )
            google.output_path = str(output_path)
            google.app_list = ["login", "drive"]
            google.update = True
            google.overwrite = False
            calls = []

            def record_call(application_name, output_file, overwrite=False, only_after_datetime=None):
                calls.append((application_name, only_after_datetime))
                return 0, 0

            google._get_activity_logs = record_call
            google.get_logs(from_date=None)

        self.assertIsNotNone(calls[0][1])
        self.assertEqual(calls[1], ("drive", None))


if __name__ == "__main__":
    unittest.main()
