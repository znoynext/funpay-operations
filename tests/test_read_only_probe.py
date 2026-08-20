from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path

from funpay_operations.database import Database
from funpay_operations.funpay import (
    FunPayAccessDenied,
    FunPayDialog,
    FunPayLotDetails,
    FunPayNetworkUnavailable,
    FunPayProfile,
    FunPayProtocolError,
    FunPayRateLimited,
    FunPaySessionExpired,
    MockFunPayClient,
)
from funpay_operations.lot_discovery import OwnLotRegistryRepository
from funpay_operations.read_only_probe import (
    MutationAttemptBlocked,
    ProbeErrorCode,
    ProbeMutationTrap,
    ProbeReadBoundary,
    ProbeRequestResult,
    ProbeState,
    ReadOnlyFunPayProbe,
    ReadOnlyProbeRepository,
    SanitizedProbeResult,
    render_safe_probe_result,
)


SECRET = "synthetic-golden-key-must-never-cross-boundary"
PRIVATE_LOT_ID = "private-lot-id-982734"
PRIVATE_BUYER = "PrivateBuyerName"


class RaisingClient(MockFunPayClient):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    def get_profile(self) -> FunPayProfile:
        self.calls.append("get_profile")
        raise self.error


class ReadOnlyProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary_directory.name) / "state.sqlite3")
        self.database.initialize()
        self.now = 2_000_000_000.0
        self.repository = ReadOnlyProbeRepository(
            self.database, cooldown_seconds=60, clock=lambda: self.now
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_probe_boundary_exposes_only_required_reads(self) -> None:
        trap = ProbeMutationTrap()
        boundary = ProbeReadBoundary(MockFunPayClient(), trap)
        public = {name for name in dir(boundary) if not name.startswith("_")}
        self.assertTrue({"get_profile", "get_own_lot_details", "get_dialogs"}.issubset(public))
        self.assertTrue({
            "send_reply", "send_message", "update_price", "create_lot", "bump", "raise_lot",
        }.isdisjoint(public))
        self.assertNotIn("check_authorization", public)

    def test_mutation_trap_fails_closed(self) -> None:
        trap = ProbeMutationTrap()
        boundary = ProbeReadBoundary(MockFunPayClient(), trap)
        with self.assertRaises(MutationAttemptBlocked):
            getattr(boundary, "update_price")
        self.assertEqual(trap.attempts, 1)

    def test_success_uses_one_profile_lots_and_dialogs_operation(self) -> None:
        client = MockFunPayClient(
            profile=FunPayProfile("owner-id", "owner-name", True),
            own_lot_details=(
                _lot(PRIVATE_LOT_ID, "Mythic+ +10 EU self-play x1"),
                _lot("unmanaged-id", "Raid boost EU"),
                _lot("ambiguous-id", "Mythic+ +10 +12 EU self-play x1"),
            ),
            dialogs=(FunPayDialog("dialog-private", "buyer-private", PRIVATE_BUYER, None),),
        )
        result = self._run(client)
        self.assertEqual(result.state, ProbeState.SUCCEEDED)
        self.assertEqual((result.own_lots_total, result.mythic_plus_count), (3, 1))
        self.assertEqual((result.unmanaged_count, result.ambiguous_count), (1, 1))
        self.assertEqual(result.dialogs_count, 1)
        self.assertEqual(client.calls, ["get_profile", "get_own_lot_details", "get_dialogs"])
        self.assertEqual(result.mutation_attempts, 0)
        self.assertEqual(result.secrets_exposed, 0)

    def test_real_registry_keeps_ids_but_sanitized_state_does_not(self) -> None:
        result = self._run(MockFunPayClient(
            own_lot_details=(_lot(PRIVATE_LOT_ID, f"Mythic+ +10 EU self-play x1 {SECRET}"),),
            dialogs=(FunPayDialog("private-dialog", "private-buyer-id", PRIVATE_BUYER, None),),
        ))
        self.assertEqual(OwnLotRegistryRepository(self.database).list()[0].details.lot_id, PRIVATE_LOT_ID)
        safe = repr(result) + render_safe_probe_result(result)
        self.assertNotIn(PRIVATE_LOT_ID, safe)
        self.assertNotIn(PRIVATE_BUYER, safe)
        self.assertNotIn(SECRET, safe)
        with self.database.session() as connection:
            stored = dict(connection.execute("SELECT * FROM read_only_probe_state").fetchone())
        serialized = json.dumps(stored, ensure_ascii=False)
        self.assertNotIn(PRIVATE_LOT_ID, serialized)
        self.assertNotIn(PRIVATE_BUYER, serialized)
        self.assertNotIn(SECRET, serialized)

    def test_sanitized_result_schema_contains_no_private_fields(self) -> None:
        names = {item.name for item in fields(SanitizedProbeResult)}
        for forbidden in (
            "golden_key", "golden_seal", "token", "lot_id", "dialog_id", "buyer",
            "message", "profile_id", "seller_id",
        ):
            self.assertNotIn(forbidden, names)

    def test_expired_and_false_authorization_stop_before_other_reads(self) -> None:
        for client in (
            RaisingClient(FunPaySessionExpired("synthetic")),
            MockFunPayClient(profile=FunPayProfile("owner", "owner", False)),
        ):
            with self.subTest(client=type(client).__name__):
                self.now += 61
                result = self._run(client)
                self.assertEqual(result.error_code, ProbeErrorCode.AUTHORIZATION_REQUIRED)
                self.assertFalse(result.authorization_ok)
                self.assertNotIn("get_own_lot_details", client.calls)
                self.assertNotIn("get_dialogs", client.calls)

    def test_network_rate_limit_access_denial_and_protocol_errors_are_sanitized(self) -> None:
        scenarios = (
            (FunPayNetworkUnavailable("network details"), ProbeErrorCode.NETWORK_UNAVAILABLE),
            (FunPayRateLimited("429 details"), ProbeErrorCode.RATE_LIMITED),
            (FunPayAccessDenied("403 details"), ProbeErrorCode.ACCESS_DENIED),
            (FunPayProtocolError("raw response contents"), ProbeErrorCode.PROTOCOL_CHANGED),
        )
        for error, expected in scenarios:
            with self.subTest(expected=expected):
                self.now += 61
                result = self._run(RaisingClient(error))
                self.assertEqual(result.state, ProbeState.FAILED)
                self.assertEqual(result.error_code, expected)
                self.assertNotIn(str(error), repr(result))

    def test_malformed_adapter_shape_stops_before_later_reads(self) -> None:
        class MalformedClient(MockFunPayClient):
            def get_own_lot_details(self) -> tuple[FunPayLotDetails, ...]:
                self.calls.append("get_own_lot_details")
                return [SECRET]  # type: ignore[return-value]

        client = MalformedClient()
        result = self._run(client)
        self.assertEqual(result.error_code, ProbeErrorCode.PROTOCOL_CHANGED)
        self.assertNotIn("get_dialogs", client.calls)
        self.assertNotIn(SECRET, repr(result))

    def test_duplicate_concurrent_request_and_rate_limit_are_atomic(self) -> None:
        self.assertEqual(self.repository.request(), ProbeRequestResult.ACCEPTED)
        self.assertEqual(self.repository.request(), ProbeRequestResult.ALREADY_RUNNING)
        self.assertIsNotNone(self.repository.claim())
        self.assertEqual(self.repository.request(), ProbeRequestResult.ALREADY_RUNNING)
        self.repository.save(SanitizedProbeResult(
            _stamp(self.now), _stamp(self.now), _stamp(self.now), ProbeState.SUCCEEDED,
        ))
        self.assertEqual(self.repository.request(), ProbeRequestResult.RATE_LIMITED)
        self.now += 61
        self.assertEqual(self.repository.request(), ProbeRequestResult.ACCEPTED)

    def test_interrupted_running_probe_is_failed_only_by_background_recovery(self) -> None:
        self.assertEqual(self.repository.request(), ProbeRequestResult.ACCEPTED)
        self.assertIsNotNone(self.repository.claim())
        self.assertTrue(self.repository.recover_interrupted())
        recovered = self.repository.load()
        self.assertEqual(recovered.state, ProbeState.FAILED)
        self.assertEqual(recovered.error_code, ProbeErrorCode.INTERNAL_ERROR)
        self.assertEqual(self.repository.request(), ProbeRequestResult.ACCEPTED)

    def test_copy_safe_success_lists_all_disabled_live_gates(self) -> None:
        text = render_safe_probe_result(self._run(MockFunPayClient()))
        for gate in (
            "Price writes: DISABLED", "Lot writes: DISABLED", "Raise: DISABLED",
            "Auto-reply: DISABLED", "Telegram replies: DISABLED", "Automation: DISABLED",
        ):
            self.assertIn(gate, text)
        self.assertIn("Mutation attempts: 0", text)
        self.assertIn("Secrets exposed: 0", text)
        self.assertNotIn("Delves", text)

    def _run(self, client: MockFunPayClient) -> SanitizedProbeResult:
        self.assertEqual(self.repository.request(), ProbeRequestResult.ACCEPTED)
        trap = ProbeMutationTrap()
        result = ReadOnlyFunPayProbe(
            ProbeReadBoundary(client, trap), OwnLotRegistryRepository(self.database),
            self.repository, trap=trap, build_sha="a" * 40, clock=lambda: self.now,
        ).run_pending()
        self.assertIsNotNone(result)
        return result


def _lot(lot_id: str, title: str) -> FunPayLotDetails:
    return FunPayLotDetails(
        lot_id, title, 100_000, "RUB", "private-owner", "node", True,
        title, title, None, False, {}, {}, (),
    )


def _stamp(epoch: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds")


if __name__ == "__main__":
    unittest.main()
