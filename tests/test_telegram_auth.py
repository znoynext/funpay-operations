from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from funpay_operations.telegram import TelegramUpdate
from funpay_operations.telegram_auth import LocalFunPayAuthRequest, TelegramFunPayAuthRouter
from funpay_operations.windows_infra import resolve_windows_paths
from tests.test_telegram import InMemoryStates


class TelegramFunPayAuthTests(unittest.TestCase):
    def _paths(self, directory: str):
        paths = resolve_windows_paths(Path(directory))
        paths.application.mkdir(parents=True)
        for name in ("funpay-operations.exe", "funpay-operations-cli.exe", "funpay-operations-setup.exe"):
            (paths.application / name).write_bytes(b"generic")
        return paths

    def test_confirmed_owner_starts_only_the_installed_setup_with_fixed_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, states, calls = self._paths(directory), InMemoryStates(), []
            request = LocalFunPayAuthRequest(
                paths, states, launcher=lambda *args, **kwargs: calls.append((args, kwargs)),
                interactive_available=lambda: True, now=lambda: 100,
            )
            router = TelegramFunPayAuthRouter((17,), request)

            reply = router.handle(TelegramUpdate(1, 17, 17, None, callback_data="auth:funpay", reply_to_message_id=8))

            self.assertIsNotNone(reply)
            self.assertIn("открыто", reply.text)
            self.assertEqual(calls[0][0][0], [str(paths.application / "funpay-operations-setup.exe"), "--funpay-auth"])
            self.assertFalse(calls[0][1]["shell"])
            self.assertNotIn("golden", reply.text)

    def test_unauthorized_user_cannot_open_local_auth_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, states, calls = self._paths(directory), InMemoryStates(), []
            request = LocalFunPayAuthRequest(paths, states, launcher=lambda *args, **kwargs: calls.append(args), interactive_available=lambda: True)
            router = TelegramFunPayAuthRouter((17,), request)

            self.assertIsNone(router.handle(TelegramUpdate(1, 99, 99, None, callback_data="auth:funpay")))
            self.assertEqual(calls, [])

    def test_rate_limit_and_noninteractive_desktop_are_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, states, calls = self._paths(directory), InMemoryStates(), []
            request = LocalFunPayAuthRequest(
                paths, states, launcher=lambda *args, **kwargs: calls.append(args), interactive_available=lambda: True, now=lambda: 100,
            )
            self.assertTrue(request.request().started)
            self.assertFalse(request.request().started)
            unavailable = LocalFunPayAuthRequest(paths, states, interactive_available=lambda: False)
            self.assertIn("после входа", unavailable.request().message)
            self.assertEqual(len(calls), 1)
