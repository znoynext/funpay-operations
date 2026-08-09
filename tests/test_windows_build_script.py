from __future__ import annotations

from pathlib import Path
import unittest


class WindowsBuildScriptTests(unittest.TestCase):
    def test_build_installs_the_checked_out_source_without_network_build_isolation(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
        self.assertIn("pip install --no-build-isolation -e '.[build]'", script)
        self.assertIn("--noconsole --paths src --name funpay-operations", script)
        self.assertIn("--console --paths src --name funpay-operations-cli", script)
