import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
DITTO = ROOT / "ditto.py"
SPEC = importlib.util.spec_from_file_location("ditto_incremental_freeze", DITTO)
ditto = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ditto)


def message(text, timestamp):
    return json.dumps({
        "type": "response_item",
        "timestamp": timestamp,
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    }) + "\n"


def no_signal_report(segment):
    return {
        "schema_version": ditto.REPORT_SCHEMA_VERSION,
        "segment_hash": segment["segment_hash"],
        "coverage": {
            "session_ids": [item["session_id"] for item in segment["session_versions"]],
            "sources": [segment["source"]],
            "first_date": segment["first_date"],
            "last_date": segment["last_date"],
            "source_tokens": segment["source_tokens"],
        },
        "domain_coverage": {domain: "no-signal" for domain in ditto.VALID_DOMAINS},
        "evidence": [],
    }


class IncrementalFreezeReuseTest(unittest.TestCase):
    def test_second_freeze_reuses_unchanged_report_and_plans_only_changed_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / ".codex" / "sessions"
            logs.mkdir(parents=True)
            changed_source = logs / "changed.jsonl"
            unchanged_source = logs / "unchanged.jsonl"
            changed_source.write_text(
                message("specific approval preference", "2026-01-01T00:00:00Z"),
                encoding="utf-8",
            )
            unchanged_source.write_text(
                message("preserve unrelated state", "2026-01-02T00:00:00Z"),
                encoding="utf-8",
            )
            home = root / "private"
            args = ditto.build_plugin_parser().parse_args([
                "freeze", "--path", str(logs), "--ditto-home", str(home),
            ])

            with mock.patch.object(ditto, "DEEP_SEGMENT_TOKENS", 1):
                first = ditto.freeze_plugin_run(args)
                first_reports = {}
                for segment in first["selected_segments"]:
                    report_path = ditto.store_report(no_signal_report(segment), str(home), segment)
                    first_reports[segment["segment_hash"]] = Path(report_path).read_bytes()

                with changed_source.open("a", encoding="utf-8") as handle:
                    handle.write(message("new preference after approval", "2026-01-03T00:00:00Z"))

                second = ditto.freeze_plugin_run(args)

            changed_session_id = ditto.stable_session_id(changed_source)
            unchanged_session_id = ditto.stable_session_id(unchanged_source)

            self.assertEqual(2, len(first["selected_segments"]))
            self.assertEqual(2, len(second["selected_segments"]))
            first_by_session = {
                version["session_id"]: segment
                for segment in first["selected_segments"]
                for version in segment["session_versions"]
            }
            second_by_session = {
                version["session_id"]: segment
                for segment in second["selected_segments"]
                for version in segment["session_versions"]
            }
            expected_sessions = {changed_session_id, unchanged_session_id}
            self.assertEqual(expected_sessions, set(first_by_session))
            self.assertEqual(expected_sessions, set(second_by_session))
            self.assertEqual(2, len(first_by_session))
            self.assertEqual(2, len(second_by_session))

            changed_first_hash = first_by_session[changed_session_id]["segment_hash"]
            changed_second_hash = second_by_session[changed_session_id]["segment_hash"]
            unchanged_first_hash = first_by_session[unchanged_session_id]["segment_hash"]
            unchanged_second_hash = second_by_session[unchanged_session_id]["segment_hash"]
            self.assertNotEqual(changed_first_hash, changed_second_hash)
            self.assertEqual(unchanged_first_hash, unchanged_second_hash)
            self.assertEqual(
                first_reports[unchanged_first_hash],
                Path(ditto.report_cache_path(str(home), unchanged_second_hash)).read_bytes(),
            )
            self.assertEqual(1, second["cached_segments"])
            self.assertEqual(1, second["uncached_segments"])
            self.assertEqual(1, second["planned_worker_calls"])
            self.assertEqual(1, second["planned_reducer_calls"])
            self.assertTrue(second_by_session[unchanged_session_id]["cached"])
            self.assertFalse(second_by_session[changed_session_id]["cached"])


if __name__ == "__main__":
    unittest.main()
