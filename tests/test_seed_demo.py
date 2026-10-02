from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from _support import ROOT


class SeedDemoTests(unittest.TestCase):
    def test_seed_script_builds_consistent_demo_database(self) -> None:
        with TemporaryDirectory() as temp:
            database = Path(temp) / "demo.sqlite3"
            env = dict(os.environ)
            env["PYTHONPATH"] = str(ROOT / "src")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "seed_demo.py"),
                    "--db",
                    str(database),
                ],
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(json.loads(completed.stdout)["seeded"])
            with closing(sqlite3.connect(database)) as connection:
                counts = tuple(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("endpoints", "events", "deliveries")
                )
            self.assertEqual(counts, (4, 6, 8))


if __name__ == "__main__":
    unittest.main()

