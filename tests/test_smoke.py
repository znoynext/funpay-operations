from __future__ import annotations

import io
import unittest

from funpay_operations.funpay import FunPayError
from funpay_operations.smoke import run_smoke_test


class SmokeClient:
    def __init__(self, *, local_session: bool = True, authorization: bool = True, fail: bool = False) -> None:
        self.local_session = local_session
        self.authorization = authorization
        self.fail = fail
        self.closed = False

    def has_local_session(self) -> bool:
        return self.local_session

    def check_authorization(self) -> bool:
        return self.authorization

    def get_profile(self) -> object:
        if self.fail:
            raise FunPayError("private detail")
        return object()

    def get_own_lots(self) -> tuple[object, ...]:
        return (object(),)

    def get_dialogs(self) -> tuple[object, ...]:
        return (object(), object())

    def close(self) -> None:
        self.closed = True


class SmokeTests(unittest.TestCase):
    def test_success_output_has_counts_but_no_private_data(self) -> None:
        client = SmokeClient()
        output = io.StringIO()
        self.assertEqual(run_smoke_test(client, output=output), 0)  # type: ignore[arg-type]
        self.assertEqual(output.getvalue().strip(), "smoke-test: local_session=present authorization=ok profile=ok own_lots=1 dialogs=2 closed=ok")
        self.assertTrue(client.closed)

    def test_missing_or_failed_session_is_sanitized(self) -> None:
        output = io.StringIO()
        self.assertEqual(run_smoke_test(SmokeClient(local_session=False), output=output), 1)  # type: ignore[arg-type]
        self.assertEqual(output.getvalue().strip(), "smoke-test: local_session=missing_or_invalid")

        output = io.StringIO()
        self.assertEqual(run_smoke_test(SmokeClient(fail=True), output=output), 1)  # type: ignore[arg-type]
        self.assertNotIn("private detail", output.getvalue())
