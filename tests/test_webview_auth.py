from __future__ import annotations

import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from funpay_operations.setup_services import FunPaySetupService
from funpay_operations.webview_auth import (
    AUTH_HELPER_NAME,
    AUTH_PROFILE_PREFIX,
    AUTH_RESULT_NAME,
    AuthSessionCandidate,
    WebView2AuthLauncher,
    WebView2RuntimeUnavailable,
)
from funpay_operations.windows_infra import resolve_windows_paths
from tests.test_setup_center import FakeFunPayClient, MemorySecretStore


class WebViewAuthTests(unittest.TestCase):
    def _launcher(self, paths, *, payload: object | None = None, code: int = 0) -> WebView2AuthLauncher:
        paths.application.mkdir(parents=True)
        (paths.application / AUTH_HELPER_NAME).write_bytes(b"generic helper")

        def runner(command: list[str], _cwd: Path) -> int:
            if command[-1] == "--runtime-status":
                return code
            profile = Path(command[2])
            if payload is not None:
                (profile / AUTH_RESULT_NAME).write_text(
                    base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii"), encoding="ascii"
                )
            return code

        return WebView2AuthLauncher(paths, runner=runner, unprotector=lambda payload: payload)

    def test_valid_candidate_is_selected_from_dpapi_result_and_profile_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = resolve_windows_paths(Path(directory))
            launcher = self._launcher(paths, payload={"golden_key": "key", "golden_seal": "seal"})

            candidate = launcher.acquire()

            self.assertEqual((candidate.golden_key, candidate.golden_seal), ("key", "seal"))
            self.assertFalse((candidate.profile / AUTH_RESULT_NAME).exists())
            self.assertTrue(launcher.cleanup(candidate.profile))
            self.assertFalse(candidate.profile.exists())

    def test_runtime_unavailable_never_creates_a_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = resolve_windows_paths(Path(directory))
            launcher = self._launcher(paths, code=2)

            with self.assertRaises(WebView2RuntimeUnavailable):
                launcher.acquire()

            self.assertFalse(paths.data.exists())

    def test_candidate_with_other_cookie_fields_is_rejected_and_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = resolve_windows_paths(Path(directory))
            launcher = self._launcher(paths, payload={"golden_key": "key", "golden_seal": "seal", "other": "ignored"})

            with self.assertRaisesRegex(Exception, "invalid format"):
                launcher.acquire()

            self.assertEqual(list(paths.data.glob(f"{AUTH_PROFILE_PREFIX}*")), [])

    def test_cleanup_retries_a_locked_temp_profile_on_next_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = resolve_windows_paths(Path(directory))
            paths.data.mkdir(parents=True)
            profile = paths.data / f"{AUTH_PROFILE_PREFIX}locked"
            profile.mkdir()
            launcher = self._launcher(paths)

            with patch("funpay_operations.webview_auth.shutil.rmtree", side_effect=OSError("locked")):
                self.assertFalse(launcher.cleanup_pending())
            self.assertTrue(profile.exists())
            self.assertTrue(launcher.cleanup_pending())
            self.assertFalse(profile.exists())

    def test_cleanup_retries_a_short_lived_webview_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = resolve_windows_paths(Path(directory))
            profile = paths.data / f"{AUTH_PROFILE_PREFIX}locked"
            profile.mkdir(parents=True)
            launcher = self._launcher(paths)
            real_rmtree = __import__("shutil").rmtree
            attempts = 0

            def temporary_lock(target: Path) -> None:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise OSError("locked")
                real_rmtree(target)

            with patch("funpay_operations.webview_auth.shutil.rmtree", side_effect=temporary_lock):
                with patch("funpay_operations.webview_auth.time.sleep"):
                    self.assertTrue(launcher.cleanup(profile))

            self.assertFalse(profile.exists())

    def test_service_saves_only_after_read_only_verification_then_cleans_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = resolve_windows_paths(Path(directory))
            store = MemorySecretStore(paths.secrets)
            profile = paths.data / f"{AUTH_PROFILE_PREFIX}candidate"
            profile.mkdir(parents=True)

            class FakeLauncher:
                def __init__(self) -> None:
                    self.cleaned: list[Path] = []

                def acquire(self) -> AuthSessionCandidate:
                    return AuthSessionCandidate("key", "seal", profile)

                def cleanup(self, target: Path) -> bool:
                    self.cleaned.append(target)
                    return True

            launcher = FakeLauncher()
            service = FunPaySetupService(
                paths, store_factory=lambda _path: store, client_factory=lambda _value: FakeFunPayClient(True),
            )

            result = service.authorize_with_webview(launcher)  # type: ignore[arg-type]

            self.assertTrue(result.ok)
            self.assertIn("funpay_session", store.values)
            self.assertEqual(launcher.cleaned, [profile])
            self.assertNotIn("key", result.message)

    def test_existing_valid_session_is_checked_without_replacing_dpapi_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = resolve_windows_paths(Path(directory))
            store = MemorySecretStore(paths.secrets)
            store.set("funpay_session", "saved-session")
            service = FunPaySetupService(
                paths, store_factory=lambda _path: store, client_factory=lambda _value: FakeFunPayClient(True),
            )

            result = service.verify_existing()

            self.assertTrue(result.ok)
            self.assertEqual(store.get("funpay_session"), "saved-session")

    def test_helper_source_uses_cookie_manager_and_never_reads_a_browser_database(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "src" / "FunPayOperations.AuthHelper" / "Program.cs").read_text(encoding="utf-8")
        self.assertIn('GetCookiesAsync(FunPayUri)', source)
        self.assertIn('cookie.Name is "golden_key" or "golden_seal"', source)
        self.assertIn("ProtectedData.Protect", source)
        self.assertIn("AllowedFunPayUrl", source)
        self.assertIn('"id.vk.com"', source)
        self.assertIn('"oauth.vk.com"', source)
        self.assertNotIn("document.cookie", source)
        self.assertNotIn("ChromeDriver", source)
        self.assertNotIn("Selenium", source)

    def test_helper_smoke_has_a_bounded_explicit_close_path(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "src" / "FunPayOperations.AuthHelper" / "Program.cs").read_text(encoding="utf-8")

        self.assertIn("_smokeTimeout", source)
        self.assertIn("Finish(0)", source)
        self.assertIn("Finish(3)", source)
        self.assertIn("BeginInvoke((MethodInvoker)Close)", source)
