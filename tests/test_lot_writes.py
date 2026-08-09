from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from funpay_operations.lot_writes import (
    CapabilityState,
    LotWriteCapability,
    LotWriteOutcome,
    LotWriteValidationError,
    MockLotWriteClient,
    NativeLotWriteClient,
)


class FakeEditor:
    def __init__(self, result: object = True) -> None:
        self.result = result
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def change_lot_price(self, *arguments: object) -> object:
        self.calls.append(("update_price", arguments))
        return self.result

    async def change_lot_short_desc(self, *arguments: object) -> object:
        self.calls.append(("update_title", arguments))
        return self.result

    async def change_lot_desc(self, *arguments: object) -> object:
        self.calls.append(("update_description", arguments))
        return self.result

    async def toggle_on_lot(self, *arguments: object) -> object:
        self.calls.append(("enable_lot", arguments))
        return self.result

    async def toggle_off_lot(self, *arguments: object) -> object:
        self.calls.append(("disable_lot", arguments))
        return self.result


class FakeLotManager:
    def __init__(self, result: object = True) -> None:
        self.result = result
        self.calls: list[str] = []

    async def raise_lots(self) -> object:
        self.calls.append("bump_raise")
        return [] if self.result is True else self.result

    async def get_node_editor_data(self, node_id: str) -> object:
        return SimpleNamespace(fields=[
            SimpleNamespace(key="price", value=None), SimpleNamespace(key="amount", value=None),
            SimpleNamespace(key="fields[summary][ru]", value=None), SimpleNamespace(key="fields[summary][en]", value=None),
        ])

    async def create_lot(self, editor: object) -> object:
        self.calls.append("create_lot")
        return self.result


class FakeReadClient:
    def __init__(self, *, result: object = True, timeout: bool = False) -> None:
        self.editor = FakeEditor(result)
        self.lot = FakeLotManager(result)
        self.timeout = timeout
        self.run_calls = 0

    def _run(self, action: object) -> object:
        self.run_calls += 1
        if self.timeout:
            raise TimeoutError
        tools = SimpleNamespace(account=SimpleNamespace(editor=self.editor, lot=self.lot))
        return asyncio.run(action(tools))  # type: ignore[operator]


class LotWriteTests(unittest.TestCase):
    def client(self, mode: str, *, session: bool = True, result: object = True, timeout: bool = False,
               live_execution_enabled: bool = False) -> tuple[NativeLotWriteClient, FakeReadClient]:
        read = FakeReadClient(result=result, timeout=timeout)
        return (
            NativeLotWriteClient(
                read, operation_mode=mode, live_session_available=lambda: session,
                live_execution_enabled=live_execution_enabled,
            ),
            read,
        )

    def test_safe_blocks_writes_without_calling_fpx(self) -> None:
        client, read = self.client("safe")
        result = client.update_price("lot", "12.34")
        self.assertEqual(result.outcome, LotWriteOutcome.SKIPPED)
        self.assertEqual(read.run_calls, 0)

    def test_dry_run_builds_a_plan_without_network_send(self) -> None:
        client, read = self.client("dry_run", session=False)
        result = client.update_title("lot", "RU title", "EN title")
        self.assertEqual(result.outcome, LotWriteOutcome.REQUESTED)
        self.assertEqual(result.plan.fpx_method, "account.editor.change_lot_short_desc")
        self.assertEqual(read.run_calls, 0)

    def test_capability_detection_is_honest_about_session_and_scope(self) -> None:
        unavailable, _ = self.client("safe", session=False)
        supported, _ = self.client("safe", session=True)
        self.assertEqual(unavailable.capabilities()[LotWriteCapability.UPDATE_PRICE].state, CapabilityState.UNAVAILABLE_WITHOUT_LIVE_SESSION)
        self.assertEqual(unavailable.capabilities()[LotWriteCapability.UPDATE_FIELDS].state, CapabilityState.UNSUPPORTED)
        self.assertEqual(supported.capabilities()[LotWriteCapability.BUMP_RAISE].state, CapabilityState.SUPPORTED)
        self.assertIn("all owned", supported.capabilities()[LotWriteCapability.BUMP_RAISE].detail)

    def test_unsupported_generic_field_update_is_reported(self) -> None:
        client, read = self.client("dry_run")
        result = client.update_fields("lot", {"amount": "2"})
        self.assertEqual(result.outcome, LotWriteOutcome.UNSUPPORTED)
        self.assertEqual(read.run_calls, 0)

    def test_timeout_and_malformed_response_fail_in_memory_live_adapter(self) -> None:
        timeout_client, _ = self.client("live", timeout=True, live_execution_enabled=True)
        malformed_client, _ = self.client("live", result=None, live_execution_enabled=True)
        self.assertEqual(timeout_client.update_price("lot", "12").outcome, LotWriteOutcome.FAILED)
        self.assertEqual(malformed_client.update_price("lot", "12").outcome, LotWriteOutcome.FAILED)

    def test_duplicate_operation_is_not_dispatched_twice(self) -> None:
        client, read = self.client("dry_run")
        first = client.disable_lot("lot", operation_key="same-operation")
        second = client.disable_lot("lot", operation_key="same-operation")
        self.assertEqual(first.outcome, LotWriteOutcome.REQUESTED)
        self.assertEqual(second.outcome, LotWriteOutcome.SKIPPED)
        self.assertEqual(read.run_calls, 0)

    def test_validation_rejects_invalid_values_and_sensitive_fields(self) -> None:
        client, _ = self.client("dry_run")
        with self.assertRaises(LotWriteValidationError):
            client.update_price("lot", "NaN")
        with self.assertRaises(LotWriteValidationError):
            client.update_fields("lot", {"secrets": "no"})
        with self.assertRaises(LotWriteValidationError):
            client.create_lot("node", {"price": "1"})

    def test_live_is_architecturally_ready_but_hard_blocked(self) -> None:
        client, read = self.client("live", live_execution_enabled=False)
        result = client.enable_lot("lot")
        self.assertEqual(result.outcome, LotWriteOutcome.VERIFICATION_REQUIRED)
        self.assertEqual(read.run_calls, 0)

    def test_in_memory_fpx_success_and_verification_requirement(self) -> None:
        client, read = self.client("live", live_execution_enabled=True)
        price = client.update_price("lot", "12")
        enable = client.enable_lot("lot")
        bump = client.bump_raise()
        self.assertEqual(price.outcome, LotWriteOutcome.SUCCEEDED)
        self.assertEqual(enable.outcome, LotWriteOutcome.VERIFICATION_REQUIRED)
        self.assertEqual(bump.outcome, LotWriteOutcome.VERIFICATION_REQUIRED)
        self.assertEqual(read.run_calls, 3)

    def test_mock_success_and_failure_are_deterministic(self) -> None:
        success = MockLotWriteClient()
        failed = MockLotWriteClient(outcomes={LotWriteCapability.UPDATE_PRICE: LotWriteOutcome.FAILED})
        self.assertEqual(success.update_price("lot", "12").outcome, LotWriteOutcome.SUCCEEDED)
        self.assertEqual(failed.update_price("lot", "12").outcome, LotWriteOutcome.FAILED)
