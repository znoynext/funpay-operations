"""Local, user-driven WebView2 authentication hand-off for FunPay.

The C# helper owns the embedded browser and is deliberately constrained to
FunPay's HTTPS origin.  It writes exactly two selected cookies into a
short-lived DPAPI-protected result file; this module never reads a browser
database and never sends a cookie through stdout, environment variables, or a
command line.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .setup_wizard import SecretStoreError, unprotect_for_current_user
from .windows_infra import WindowsPaths


AUTH_HELPER_NAME = "funpay-operations-auth.exe"
AUTH_PROFILE_PREFIX = "auth-temp-"
AUTH_RESULT_NAME = "session-result.dpapi"


class WebViewAuthError(RuntimeError):
    """A safe, credential-free authentication hand-off error."""


class WebView2RuntimeUnavailable(WebViewAuthError):
    """The official Evergreen WebView2 Runtime is not installed."""


class WebViewAuthCancelled(WebViewAuthError):
    """The user closed the local authentication window without a session."""


@dataclass(repr=False)
class AuthSessionCandidate:
    """In-memory candidate selected from the isolated WebView2 profile."""

    golden_key: str
    golden_seal: str
    profile: Path


Runner = Callable[[list[str], Path], int]


class WebView2AuthLauncher:
    """Launch only the installed helper with a freshly-created owned profile."""

    def __init__(self, paths: WindowsPaths, *, runner: Runner | None = None) -> None:
        self.paths = paths
        self._runner = runner or self._run_process

    @property
    def helper(self) -> Path:
        return self.paths.application / AUTH_HELPER_NAME

    def runtime_available(self) -> bool:
        if not _nonempty_file(self.helper):
            return False
        return self._runner([str(self.helper), "--runtime-status"], self.helper.parent) == 0

    def acquire(self) -> AuthSessionCandidate:
        """Open the local authentication window and return its selected cookies.

        The caller must always call :meth:`cleanup` after read-only validation.
        """

        self.cleanup_pending()
        if not _nonempty_file(self.helper):
            raise WebViewAuthError("local WebView2 authentication component is unavailable")
        if not self.runtime_available():
            raise WebView2RuntimeUnavailable("Microsoft Edge WebView2 Runtime is unavailable")
        self.paths.data.mkdir(parents=True, exist_ok=True)
        profile = Path(tempfile.mkdtemp(prefix=AUTH_PROFILE_PREFIX, dir=self.paths.data))
        return_code = self._runner([str(self.helper), "--profile-dir", str(profile)], self.helper.parent)
        if return_code == 2:
            self.cleanup(profile)
            raise WebView2RuntimeUnavailable("Microsoft Edge WebView2 Runtime is unavailable")
        if return_code != 0:
            self.cleanup(profile)
            raise WebViewAuthCancelled("local FunPay authentication was cancelled")
        try:
            golden_key, golden_seal = self._read_candidate(profile)
            return AuthSessionCandidate(golden_key, golden_seal, profile)
        except Exception:
            self.cleanup(profile)
            raise

    def smoke(self) -> bool:
        """Open a disposable WebView2 view of public funpay.com without cookies."""

        self.cleanup_pending()
        if not _nonempty_file(self.helper):
            return False
        profile = Path(tempfile.mkdtemp(prefix=AUTH_PROFILE_PREFIX, dir=self.paths.data))
        try:
            return self._runner([str(self.helper), "--profile-dir", str(profile), "--smoke"], self.helper.parent) == 0
        finally:
            self.cleanup(profile)

    def cleanup(self, profile: Path) -> bool:
        """Remove only a direct child of our auth-temp directory, best effort.

        WebView2 can release the final profile handle shortly after its helper
        process exits.  A small bounded retry keeps completed login profiles out
        of the user data directory without ever touching another directory.
        """

        if not _owned_profile(self.paths, profile):
            return False
        for attempt in range(5):
            try:
                shutil.rmtree(profile)
            except OSError:
                if attempt == 4:
                    return False
                time.sleep(0.25)
                continue
            return not profile.exists()
        return False

    def cleanup_pending(self) -> bool:
        """Retry deletion of closed helpers' profiles on each later launch."""

        if not self.paths.data.is_dir():
            return True
        cleaned = True
        for profile in self.paths.data.glob(f"{AUTH_PROFILE_PREFIX}*"):
            if not self.cleanup(profile):
                cleaned = False
        return cleaned

    def _read_candidate(self, profile: Path) -> tuple[str, str]:
        result = profile / AUTH_RESULT_NAME
        try:
            protected = base64.b64decode(result.read_text(encoding="ascii"), validate=True)
            payload = json.loads(unprotect_for_current_user(protected).decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError, SecretStoreError) as error:
            raise WebViewAuthError("local authentication result is unavailable") from error
        finally:
            try:
                result.unlink(missing_ok=True)
            except OSError:
                pass
        if set(payload) != {"golden_key", "golden_seal"}:
            raise WebViewAuthError("local authentication result has an invalid format")
        key, seal = payload.get("golden_key"), payload.get("golden_seal")
        if not all(isinstance(value, str) and value and "\r" not in value and "\n" not in value for value in (key, seal)):
            raise WebViewAuthError("local authentication result has invalid cookies")
        return key, seal

    @staticmethod
    def _run_process(command: list[str], cwd: Path) -> int:
        try:
            completed = subprocess.run(
                command, cwd=str(cwd), check=False, shell=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as error:
            raise WebViewAuthError("local authentication component could not be started") from error
        return completed.returncode


def _nonempty_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _owned_profile(paths: WindowsPaths, profile: Path) -> bool:
    try:
        return (
            profile.parent.resolve() == paths.data.resolve()
            and profile.name.startswith(AUTH_PROFILE_PREFIX)
            and profile.is_dir()
            and not profile.is_symlink()
        )
    except OSError:
        return False
