from __future__ import annotations

from pathlib import Path
import unittest


class WindowsBuildScriptTests(unittest.TestCase):
    def test_build_installs_the_checked_out_source_without_network_build_isolation(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
        self.assertIn("pip install -r requirements.txt 'pyinstaller==6.22.0'", script)
        self.assertNotIn("pip install --no-build-isolation -e", script)
        self.assertIn("--noconsole --paths src --name funpay-operations", script)
        self.assertIn("--console --paths src --name funpay-operations-cli", script)
        self.assertIn("--noconsole --paths src --name funpay-operations-setup", script)
        self.assertIn("windows_setup_entrypoint.py", script)
        self.assertIn("FunPayOperations.AuthHelper", script)
        self.assertIn("funpay-operations-auth.exe", script)
        self.assertIn("WebView2 authentication helper build failed", script)

    def test_developer_installer_builds_installs_verifies_and_opens_setup_center(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "scripts" / "install_local_windows.ps1").read_text(encoding="utf-8")
        self.assertIn("build_windows.ps1", script)
        self.assertIn("funpay-operations-setup.exe", script)
        self.assertIn("Task Scheduler target", script)
        self.assertIn("FunPay Operations Setup.lnk", script)
        self.assertIn("Start-Process -FilePath $installedSetup", script)
