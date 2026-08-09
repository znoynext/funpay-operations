from __future__ import annotations
import tempfile
import unittest
from pathlib import Path
from funpay_operations.windows_infra import TASK_NAME, diagnostics, first_run, resolve_windows_paths, task_scheduler_command

class WindowsInfraTests(unittest.TestCase):
 def test_paths_and_first_run_preserve_existing_data(self):
  with tempfile.TemporaryDirectory() as temp:
   paths=resolve_windows_paths(Path(temp)); first_run(paths); paths.database.write_bytes(paths.database.read_bytes()); self.assertTrue(paths.database.exists()); self.assertEqual(first_run(paths)["funpay"], "skipped")
 def test_diagnostics_missing_secrets_is_not_configured(self):
  with tempfile.TemporaryDirectory() as temp: self.assertEqual(diagnostics(resolve_windows_paths(Path(temp)))["telegram"], "not_configured")
 def test_scheduler_commands_and_uninstall_preserves_data(self):
  command=task_scheduler_command(Path('C:/app/funpay.exe'), action='install'); self.assertIn(TASK_NAME, command); self.assertIn('0000:30', command); self.assertEqual(task_scheduler_command(Path('x'), action='remove')[1], '/Delete')
