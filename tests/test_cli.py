from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from _support import ROOT
from relayforge.cli import main


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "cli.sqlite3")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def invoke(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--db", self.db, *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_init_prints_one_json_value(self) -> None:
        code, stdout, stderr = self.invoke("init")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertTrue(json.loads(stdout)["initialized"])
        self.assertEqual(len(stdout.strip().splitlines()), 1)

    def test_endpoint_add_reads_secret_from_environment_but_never_echoes_it(self) -> None:
        with patch.dict(os.environ, {"HOOK_SECRET": "never-print-this"}):
            code, stdout, stderr = self.invoke(
                "endpoint",
                "add",
                "--tenant",
                "org_cli_7d83",
                "--url",
                "https://user:pass@example.test/hook?token=also-secret",
                "--secret-env",
                "HOOK_SECRET",
            )
        self.assertEqual(code, 0)
        self.assertNotIn("never-print-this", stdout + stderr)
        self.assertNotIn("also-secret", stdout + stderr)
        self.assertEqual(json.loads(stdout)["url"], "https://example.test/hook")

    def test_missing_secret_is_an_expected_json_error(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            code, stdout, stderr = self.invoke(
                "endpoint",
                "add",
                "--tenant",
                "org_cli_7d83",
                "--url",
                "https://example.test/hook",
                "--secret-env",
                "MISSING_SECRET",
            )
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["error"], "ValidationError")

    def test_event_enqueue_reads_payload_file_and_is_idempotent(self) -> None:
        payload = Path(self.temp.name) / "payload.json"
        payload.write_text('{"b": 2, "a": 1}', encoding="utf-8")
        first = self.invoke(
            "event",
            "enqueue",
            "--tenant",
            "org_cli_7d83",
            "--topic",
            "invoice.paid",
            "--key",
            "invoice-1",
            "--payload",
            str(payload),
        )
        second = self.invoke(
            "event",
            "enqueue",
            "--tenant",
            "org_cli_7d83",
            "--topic",
            "invoice.paid",
            "--key",
            "invoice-1",
            "--payload",
            str(payload),
        )
        self.assertTrue(json.loads(first[1])["created"])
        self.assertFalse(json.loads(second[1])["created"])
        self.assertEqual(json.loads(first[1])["id"], json.loads(second[1])["id"])

    def test_inspect_unknown_delivery_returns_exit_two(self) -> None:
        code, stdout, stderr = self.invoke("delivery", "inspect", "missing")
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["error"], "NotFound")


if __name__ == "__main__":
    unittest.main()
