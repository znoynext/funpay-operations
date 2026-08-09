from __future__ import annotations

from datetime import UTC, datetime
import logging
from pathlib import Path
import tempfile
import unittest

from funpay_operations.auto_reply import AUTO_REPLY_TEXT, AutoReplyService
from funpay_operations.database import Database
from funpay_operations.funpay import FunPayMessage, FunPayNetworkUnavailable, FunPayProtocolError, MockFunPayClient
from funpay_operations.lot_sync import (
    CurrentLotState,
    DescriptionConfirmation,
    DescriptionTarget,
    DesiredLotState,
    LotSyncDecision,
    LotSyncPlanner,
    MockLotSyncCoordinator,
)
from funpay_operations.lot_writes import (
    CapabilityState,
    LotWriteCapability,
    LotWriteOutcome,
    MockLotWriteClient,
)
from funpay_operations.notifications import FunPayMessageNotifier
from funpay_operations.price_safety import PriceObservationRecord, SafetyValidatedPricingEngine
from funpay_operations.price_transactions import (
    FamilyBatchStatus,
    ManagedPriceLot,
    MockCompetitorObservationAdapter,
    MockOwnLotPriceAdapter,
    PriceSnapshotRepository,
    PriceUpdateCoordinator,
    TransactionMode,
)
from funpay_operations.pricing import OwnLotPriceState, OwnLotPricingMode, PricePolicy, TrustedPriceObservation
from funpay_operations.raise_transactions import (
    MockRaiseCapabilityClient,
    RaiseAttemptRepository,
    RaiseCoordinator,
    RaiseResultStatus,
)
from funpay_operations.replies import FunPayReplyRouter
from funpay_operations.repositories import (
    AutoReplyRepository,
    DialogRepository,
    ReplyRepository,
    TaskStateRepository,
    TelegramMessageLinkRepository,
)
from funpay_operations.runtime import (
    BackgroundSupervisor,
    BackgroundTask,
    ExponentialBackoff,
    RecoveryCoordinator,
    RecoveryStep,
    SleepResumeHandler,
    TaskDisposition,
)
from funpay_operations.service_catalog import CatalogFamily, CatalogService, DesiredState
from funpay_operations.telegram import MockTelegramApi, TelegramCommandHandler, TelegramLongPollingBot, TelegramUpdate
from funpay_operations.telegram_control import (
    CompositeTelegramRouter,
    EmergencyStopGate,
    MockControlService,
    TelegramControlRouter,
)
from funpay_operations.trusted_sellers import (
    CompetitorLotMapping,
    CompetitorLotMappingRepository,
    CompetitorLotSnapshot,
    ManualSellerConfirmationAPI,
    MappingState,
    SellerFamily,
    SellerLastCheckedState,
    SellerVerificationState,
    ServiceMatchSpec,
    TrustedSeller,
    TrustedSellerRepository,
)


class RecordingReplyClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str]] = []
        self.fail = False

    def send_reply(self, dialog_id: str, buyer_nickname: str, body: str, idempotency_key: str) -> None:
        self.calls.append((dialog_id, buyer_nickname, body, idempotency_key))
        if self.fail:
            raise FunPayProtocolError("mock send failed")


class ReconnectingMockFunPay(MockFunPayClient):
    def __init__(self) -> None:
        super().__init__()
        self.fail_once = True

    def get_new_messages(self, after_message_id: str | None = None) -> tuple[FunPayMessage, ...]:
        if self.fail_once:
            self.fail_once = False
            raise FunPayNetworkUnavailable("mock disconnect")
        return super().get_new_messages(after_message_id)


class MessagingMockE2ETests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary_directory.name) / "messages.sqlite3")
        self.database.initialize()
        self.states = TaskStateRepository(self.database)
        self.api = MockTelegramApi()
        self.logger = logging.getLogger("funpay_operations.mock-e2e.messages")
        self.command_handler = TelegramCommandHandler((1001,), self.states, self.logger, auto_reply_available=True)
        self.bot = TelegramLongPollingBot(
            self.api, self.command_handler, self.states, self.logger, timeout_seconds=1
        )
        self.gate = EmergencyStopGate(self.states)
        self.reply_client = RecordingReplyClient()
        self.links = TelegramMessageLinkRepository(self.database)
        self.reply_router = FunPayReplyRouter(
            (1001,), self.links, ReplyRepository(self.database), self.reply_client,
            outbound_allowed=self.gate.permits,
        )
        self.bot.set_interaction_router(CompositeTelegramRouter(self.reply_router))
        self.funpay = ReconnectingMockFunPay()
        self.auto_reply = AutoReplyService(
            self.reply_client, AutoReplyRepository(self.database), self.states, self.logger,
            default_enabled=True, outbound_allowed=self.gate.permits,
        )
        self.notifier = FunPayMessageNotifier(
            self.funpay, self.bot, DialogRepository(self.database), self.links, self.states,
            1001, self.logger, self.auto_reply,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def message(identifier: str, dialog: str, buyer: str) -> FunPayMessage:
        return FunPayMessage(
            identifier, dialog, "incoming", "synthetic body", "2026-08-09T12:00:00Z", buyer
        )

    def test_complete_message_pipeline_is_durable_and_dialog_safe(self) -> None:
        self.notifier.sync()  # simulated disconnect; no cursor is lost
        self.notifier.sync()  # historical bootstrap completes with no messages
        first = self.message("message-a", "dialog-a", "MockBuyerA")
        second = self.message("message-b", "dialog-b", "MockBuyerB")
        self.funpay.messages = (first, first, second)
        self.notifier.sync()
        self.notifier.sync()

        notifications = self.api.sent_messages[:2]
        self.assertEqual(len(notifications), 2)
        self.assertEqual([call[0] for call in self.reply_client.calls], ["dialog-a", "dialog-b"])
        self.assertEqual([call[2] for call in self.reply_client.calls], [AUTO_REPLY_TEXT, AUTO_REPLY_TEXT])

        self.api.update_batches.append((
            TelegramUpdate(10, 1001, 1001, "reply-a", reply_to_message_id=1),
            TelegramUpdate(10, 1001, 1001, "reply-a", reply_to_message_id=1),
            TelegramUpdate(11, 1001, 1001, "reply-b", reply_to_message_id=2),
        ))
        self.bot.poll_once()
        manual = self.reply_client.calls[2:]
        self.assertEqual([(item[0], item[2]) for item in manual], [
            ("dialog-a", "reply-a"), ("dialog-b", "reply-b")
        ])

        restarted_auto = AutoReplyService(
            self.reply_client, AutoReplyRepository(self.database), self.states, self.logger,
            default_enabled=True, outbound_allowed=self.gate.permits,
        )
        restarted = FunPayMessageNotifier(
            self.funpay, self.bot, DialogRepository(self.database), self.links, self.states,
            1001, self.logger, restarted_auto,
        )
        self.funpay.messages = (self.message("message-a-2", "dialog-a", "MockBuyerA"),)
        restarted.sync()
        self.assertEqual(sum(call[2] == AUTO_REPLY_TEXT for call in self.reply_client.calls), 2)

    def test_failed_send_retry_and_emergency_stop_do_not_block_incoming(self) -> None:
        self.notifier.sync()
        self.notifier.sync()
        self.funpay.messages = (self.message("message-a", "dialog-a", "MockBuyerA"),)
        self.notifier.sync()

        self.reply_client.fail = True
        self.api.update_batches.append((TelegramUpdate(20, 1001, 1001, "retry-me", reply_to_message_id=1),))
        self.bot.poll_once()
        with self.database.session() as connection:
            attempt_id = int(connection.execute(
                "SELECT id FROM funpay_reply_attempts WHERE telegram_update_id = 20"
            ).fetchone()[0])
        first_key = self.reply_client.calls[-1][3]

        self.reply_client.fail = False
        self.api.update_batches.append((TelegramUpdate(
            21, 1001, 1001, None, callback_data=f"funpay_retry:{attempt_id}"
        ),))
        self.bot.poll_once()
        self.assertEqual(self.reply_client.calls[-1][3], first_key)

        calls_before_stop = len(self.reply_client.calls)
        self.gate.engage()
        self.api.update_batches.append((TelegramUpdate(22, 1001, 1001, "blocked", reply_to_message_id=1),))
        self.bot.poll_once()
        self.assertEqual(len(self.reply_client.calls), calls_before_stop)

        sent_before = len(self.api.sent_messages)
        self.funpay.messages = (self.message("message-b", "dialog-b", "MockBuyerB"),)
        self.notifier.sync()
        self.assertGreater(len(self.api.sent_messages), sent_before)
        self.assertEqual(len(self.reply_client.calls), calls_before_stop)


class PricingMockE2ETests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary_directory.name) / "pricing.sqlite3")
        self.database.initialize()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_trusted_repository_exact_mapping_to_verified_price_and_raise(self) -> None:
        service = _catalog_service("mplus", CatalogFamily.MYTHIC_PLUS)
        spec = ServiceMatchSpec.from_catalog(service, category="wow")
        seller_repository = TrustedSellerRepository(self.database)
        mapping_repository = CompetitorLotMappingRepository(self.database)
        confirmation = ManualSellerConfirmationAPI(seller_repository, mapping_repository)
        records: list[PriceObservationRecord] = []
        for index, seller_id in enumerate(("seller-a", "seller-b")):
            confirmation.add_mock_seller(
                seller_id, f"Mock {index}", SellerFamily.MYTHIC_PLUS,
                verification_state=SellerVerificationState.VERIFIED,
            )
            snapshot = _competitor_snapshot(seller_id, f"lot-{index}")
            mapping = confirmation.confirm_match(snapshot, (spec,))
            records.append(_observation(mapping, 10_000 + index * 100, sequence=2))

        own = MockOwnLotPriceAdapter({"own-mplus": 11_000})
        snapshots = PriceSnapshotRepository(self.database)
        price_coordinator = PriceUpdateCoordinator(
            observation_adapter=MockCompetitorObservationAdapter(tuple(records)),
            own_price_adapter=own,
            safety_engine=SafetyValidatedPricingEngine(),
            snapshots=snapshots,
            lots=(ManagedPriceLot(
                "own-mplus", SellerFamily.MYTHIC_PLUS,
                OwnLotPriceState("mplus", 11_000, "RUB", OwnLotPricingMode.AUTOMATIC),
            ),),
            sellers=seller_repository.list(),
            mappings=tuple(
                item for seller in seller_repository.list()
                for item in mapping_repository.list_for_seller(seller.seller_id)
            ),
            history=(),
            policies={"mplus": PricePolicy(1_000, 100, "RUB")},
        )
        raise_client = MockRaiseCapabilityClient()
        result = RaiseCoordinator(
            price_coordinator=price_coordinator,
            snapshots=snapshots,
            raise_client=raise_client,
            attempts=RaiseAttemptRepository(self.database),
        ).run("mock-e2e", now=datetime(2026, 8, 9, tzinfo=UTC))

        self.assertEqual(own.prices["own-mplus"], 9_900)
        self.assertEqual(result.price_transaction.batches[0].status, FamilyBatchStatus.COMPLETED)
        self.assertEqual(result.families[0].status, RaiseResultStatus.COMPLETED)
        self.assertEqual([call[0] for call in raise_client.calls], [SellerFamily.MYTHIC_PLUS])

    def test_market_scenarios_modes_and_hard_floor(self) -> None:
        scenarios = (
            ("normal", (10_000, 10_100), (), OwnLotPricingMode.AUTOMATIC, None, 1_000, 9_900, 1),
            ("fake-outlier", (5_000, 10_000, 10_100), (), OwnLotPricingMode.AUTOMATIC, None, 1_000, 9_900, 1),
            ("hard-floor", (4_000, 4_100), (), OwnLotPricingMode.AUTOMATIC, None, 5_000, 5_000, 1),
            ("fixed", (10_000, 10_100), (), OwnLotPricingMode.FIXED_PRICE, 8_500, 1_000, 8_500, 1),
            ("paused", (10_000, 10_100), (), OwnLotPricingMode.PAUSED, None, 1_000, 11_000, 0),
            ("check-only", (10_000, 10_100), (), OwnLotPricingMode.CHECK_ONLY, None, 1_000, 11_000, 0),
            ("no-reference", (), (), OwnLotPricingMode.AUTOMATIC, None, 1_000, 11_000, 0),
        )
        for name, prices, history_prices, mode, fixed, floor, expected, writes in scenarios:
            with self.subTest(name=name):
                adapter = MockOwnLotPriceAdapter({f"own-{name}": 11_000})
                coordinator = _price_coordinator(
                    self.database, adapter, service_code=name, prices=prices,
                    history_prices=history_prices, mode=mode, fixed=fixed, floor=floor,
                )
                batch = coordinator.run(TransactionMode.EXECUTE).batches[0]
                self.assertEqual(adapter.prices[f"own-{name}"], expected)
                self.assertEqual(len(adapter.write_calls), writes)
                self.assertIn(batch.status, {FamilyBatchStatus.COMPLETED, FamilyBatchStatus.BLOCKED})
                if name == "check-only":
                    self.assertEqual(batch.decisions[0].price_decision.final_target_minor, 9_900)

        single = MockOwnLotPriceAdapter({"own-single": 11_000})
        single_coordinator = _price_coordinator(
            self.database, single, service_code="single", prices=(4_000,),
            history_prices=(4_050, 4_000), floor=1_000,
        )
        self.assertEqual(single_coordinator.run(TransactionMode.EXECUTE).batches[0].status, FamilyBatchStatus.COMPLETED)
        self.assertEqual(single.prices["own-single"], 3_900)

        drop = MockOwnLotPriceAdapter({"own-drop": 11_000})
        drop_coordinator = _price_coordinator(
            self.database, drop, service_code="drop", prices=(4_000, 4_100, 10_000),
            history_prices=(10_000, 10_000, 10_000), floor=1_000,
        )
        drop_batch = drop_coordinator.run(TransactionMode.EXECUTE).batches[0]
        self.assertTrue(drop_batch.decisions[0].consensus.high_volatility_consensus)
        self.assertEqual(drop.prices["own-drop"], 3_900)

    def test_retry_rollback_and_bidirectional_family_isolation(self) -> None:
        retry_adapter = MockOwnLotPriceAdapter({"own-retry": 11_000}, stale_write_attempts={"own-retry": 1})
        retry = _price_coordinator(self.database, retry_adapter, service_code="retry", prices=(10_000, 10_100))
        self.assertEqual(retry.run(TransactionMode.EXECUTE).batches[0].status, FamilyBatchStatus.COMPLETED)
        self.assertEqual(len(retry_adapter.write_calls), 2)
        self.assertEqual(
            retry.rollback(SellerFamily.MYTHIC_PLUS, TransactionMode.ROLLBACK).status,
            FamilyBatchStatus.ROLLED_BACK,
        )
        self.assertEqual(retry_adapter.prices["own-retry"], 11_000)

        for failing_family in (SellerFamily.MYTHIC_PLUS, SellerFamily.DELVES):
            with self.subTest(failing_family=failing_family):
                database = Database(Path(self.temporary_directory.name) / f"{failing_family.value}.sqlite3")
                database.initialize()
                adapter = MockOwnLotPriceAdapter(
                    {"own-m": 11_000, "own-d": 11_000},
                    write_failures={"own-m" if failing_family is SellerFamily.MYTHIC_PLUS else "own-d"},
                )
                coordinator = _two_family_coordinator(database, adapter)
                batches = {item.family: item for item in coordinator.run(TransactionMode.EXECUTE).batches}
                self.assertEqual(batches[failing_family].status, FamilyBatchStatus.FAILED)
                other = SellerFamily.DELVES if failing_family is SellerFamily.MYTHIC_PLUS else SellerFamily.MYTHIC_PLUS
                self.assertEqual(batches[other].status, FamilyBatchStatus.COMPLETED)


class LotsMockE2ETests(unittest.TestCase):
    def test_create_reread_verify_and_repeat_are_idempotent(self) -> None:
        desired = (_desired_lot("mplus", desired_state=DesiredState.ENABLED),)
        client = MockLotWriteClient()
        coordinator = MockLotSyncCoordinator(client)
        first = coordinator.execute(desired, ())
        self.assertEqual(first.initial_plan.actions[0].decision, LotSyncDecision.CREATE_REQUIRED)
        self.assertTrue(first.verified)
        self.assertEqual(len(client.calls), 1)

        repeated = coordinator.execute(desired, first.reread_lots)
        self.assertEqual(repeated.initial_plan.actions[0].decision, LotSyncDecision.ALREADY_CORRECT)
        self.assertTrue(repeated.verified)
        self.assertEqual(len(client.calls), 1)

    def test_update_disable_enable_and_failed_verification_paths(self) -> None:
        desired = (_desired_lot("mplus", desired_state=DesiredState.ENABLED),)
        stale = (_current_lot(
            "lot-1", "mplus", title="stale", description="stale", active=False,
            fields={"level": "9"},
        ),)
        client = MockLotWriteClient()
        result = MockLotSyncCoordinator(client).execute(desired, stale)
        self.assertTrue(result.verified)
        self.assertEqual(
            {call.capability for call in client.calls},
            {
                LotWriteCapability.UPDATE_TITLE, LotWriteCapability.UPDATE_DESCRIPTION,
                LotWriteCapability.UPDATE_FIELDS, LotWriteCapability.ENABLE_LOT,
            },
        )

        disable_desired = (_desired_lot("mplus", desired_state=DesiredState.DISABLED),)
        disabled = MockLotSyncCoordinator(MockLotWriteClient()).execute(disable_desired, result.reread_lots)
        self.assertTrue(disabled.verified)
        self.assertEqual(disabled.write_results[0].capability, LotWriteCapability.DISABLE_LOT)

        failing = MockLotWriteClient(outcomes={
            LotWriteCapability.UPDATE_DESCRIPTION: LotWriteOutcome.FAILED,
        })
        failed = MockLotSyncCoordinator(failing).execute(desired, stale)
        self.assertFalse(failed.verified)

    def test_ambiguous_duplicate_and_unsupported_create_never_write(self) -> None:
        desired = (_desired_lot("mplus"),)
        unsupported_client = MockLotWriteClient(capability_states={
            LotWriteCapability.CREATE_LOT: CapabilityState.UNSUPPORTED
        })
        unsupported = MockLotSyncCoordinator(unsupported_client).execute(desired, ())
        self.assertEqual(unsupported.initial_plan.actions[0].decision, LotSyncDecision.UNSUPPORTED)
        self.assertEqual(unsupported_client.calls, [])

        unconfirmed = (_current_lot("unknown", None),)
        ambiguous_client = MockLotWriteClient()
        ambiguous = MockLotSyncCoordinator(ambiguous_client).execute(desired, unconfirmed)
        self.assertEqual(ambiguous.initial_plan.actions[0].decision, LotSyncDecision.AMBIGUOUS)
        self.assertEqual(ambiguous_client.calls, [])

        duplicates = (_current_lot("one", "mplus"), _current_lot("two", "mplus"))
        plan = LotSyncPlanner(ambiguous_client).plan(desired, duplicates)
        self.assertEqual(plan.actions[0].decision, LotSyncDecision.AMBIGUOUS)


class TelegramControlMockE2ETests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary_directory.name) / "control.sqlite3")
        self.database.initialize()
        self.states = TaskStateRepository(self.database)
        self.api = MockTelegramApi()
        self.logger = logging.getLogger("funpay_operations.mock-e2e.control")
        self.handler = TelegramCommandHandler((1001,), self.states, self.logger)
        self.bot = TelegramLongPollingBot(self.api, self.handler, self.states, self.logger, timeout_seconds=1)
        self.service = MockControlService()
        self.gate = EmergencyStopGate(self.states)
        self.bot.set_interaction_router(TelegramControlRouter((1001,), self.states, self.service, self.gate))

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_entire_menu_confirmation_allowlist_and_emergency_gate(self) -> None:
        def callback(markup: dict[str, object], label: str) -> str:
            for row in markup["inline_keyboard"]:  # type: ignore[index]
                for button in row:
                    if button["text"] == label:
                        return button["callback_data"]
            raise AssertionError(f"missing button: {label}")

        self.api.update_batches.append((TelegramUpdate(1, 1001, 1001, "💰 Цены"),))
        self.bot.poll_once()
        price_message_id = self.api.next_message_id - 1
        preview = callback(self.api.sent_messages[-1][2], "Обновить цены")
        self.api.update_batches.append((TelegramUpdate(2, 1001, 1001, None, price_message_id, preview),))
        self.bot.poll_once()
        confirm = callback(self.api.edited_messages[-1][3], "✅ Подтвердить")
        self.api.update_batches.append((TelegramUpdate(3, 1001, 1001, None, price_message_id, confirm),))
        self.bot.poll_once()
        self.assertIn(("mass_price_update", None), self.service.calls)

        self.api.update_batches.append((TelegramUpdate(4, 1001, 1001, "⚙️ Настройки"),))
        self.bot.poll_once()
        settings_message_id = self.api.next_message_id - 1
        emergency = callback(self.api.sent_messages[-1][2], "⚠️ Emergency stop")
        self.api.update_batches.append((TelegramUpdate(5, 1001, 1001, None, settings_message_id, emergency),))
        self.bot.poll_once()
        stop = callback(self.api.edited_messages[-1][3], "🛑 Остановить")
        self.api.update_batches.append((TelegramUpdate(6, 1001, 1001, None, settings_message_id, stop),))
        self.bot.poll_once()
        self.assertTrue(self.gate.active())
        self.assertTrue(self.gate.permits("incoming_notifications"))
        self.assertFalse(self.gate.permits("lot_writes"))

        calls_before = len(self.service.calls)
        self.api.update_batches.append((TelegramUpdate(7, 1001, 1001, "💰 Цены"),))
        self.bot.poll_once()
        blocked_message_id = self.api.next_message_id - 1
        preview = callback(self.api.sent_messages[-1][2], "Обновить цены")
        self.api.update_batches.append((TelegramUpdate(8, 1001, 1001, None, blocked_message_id, preview),))
        self.bot.poll_once()
        confirm = callback(self.api.edited_messages[-1][3], "✅ Подтвердить")
        self.api.update_batches.append((TelegramUpdate(9, 1001, 1001, None, blocked_message_id, confirm),))
        self.bot.poll_once()
        self.assertEqual(len(self.service.calls), calls_before)

        self.api.update_batches.append((TelegramUpdate(10, 2002, 2002, "💰 Цены"),))
        self.bot.poll_once()
        self.assertEqual(len(self.service.calls), calls_before)


class BackgroundMockE2ETests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_session_recovery_is_fail_closed_then_recovers_in_order(self) -> None:
        calls: list[str] = []
        expired = True

        async def step(name: str) -> None:
            calls.append(name)
            if name == "validate-external-sessions" and expired:
                raise RuntimeError("expired mock session")

        recovery = RecoveryCoordinator(tuple(
            RecoveryStep(name, lambda name=name: step(name)) for name in RecoveryCoordinator.ORDER
        ), logging.getLogger("funpay_operations.mock-e2e.recovery"))
        resume = SleepResumeHandler(recovery)
        resume.on_sleep()
        failed = await resume.on_resume()
        self.assertEqual(len(failed.steps), 1)
        self.assertEqual(failed.steps[0].disposition, TaskDisposition.FAILED)

        expired = False
        recovered = await resume.on_resume()
        self.assertEqual(
            [item.name for item in recovered.steps], list(RecoveryCoordinator.ORDER)
        )
        self.assertTrue(all(item.disposition is TaskDisposition.SUCCEEDED for item in recovered.steps))

    async def test_funpay_and_telegram_timeouts_are_isolated_and_retryable(self) -> None:
        attempts = {"funpay": 0, "telegram": 0, "healthy": 0}

        def transient(name: str) -> None:
            attempts[name] += 1
            if attempts[name] == 1:
                raise TimeoutError(f"mock {name} timeout")

        supervisor = BackgroundSupervisor(
            (
                BackgroundTask("funpay-message-poller", lambda: transient("funpay"), 1),
                BackgroundTask("telegram-polling-service", lambda: transient("telegram"), 1),
                BackgroundTask("healthy", lambda: transient("healthy"), 1),
            ),
            logger=logging.getLogger("funpay_operations.mock-e2e.supervisor"),
            backoff=ExponentialBackoff(1, 4),
        )
        first = await supervisor.run_once()
        self.assertTrue(all(item.disposition is TaskDisposition.FAILED for item in first))
        second = await supervisor.run_once()
        self.assertTrue(all(item.disposition is TaskDisposition.SUCCEEDED for item in second))
        await supervisor.shutdown()


def _catalog_service(code: str, family: CatalogFamily) -> CatalogService:
    variant = {
        "region": "eu", "service_format": "selfplay", "package_size": 1,
        "key_level": 10,
    }
    if family is CatalogFamily.DELVES:
        variant = {
            "region": "eu", "service_format": "selfplay", "package_size": 1,
            "tier": 8, "mode": "bountiful",
        }
    return CatalogService(
        code, family, variant, True, DesiredState.ENABLED, "template", "description", "policy", {"timed": "yes"}
    )


def _competitor_snapshot(seller_id: str, lot_id: str) -> CompetitorLotSnapshot:
    return CompetitorLotSnapshot(
        seller_id, lot_id, "Synthetic Mythic+", SellerFamily.MYTHIC_PLUS, "wow", "eu", 10,
        None, None, "selfplay", 1, {"timed": "yes"}, {"level": "10"}, {"format": ("selfplay",)},
    )


def _observation(mapping: CompetitorLotMapping, price: int, *, sequence: int) -> PriceObservationRecord:
    observation = TrustedPriceObservation(
        mapping.seller_id, mapping.competitor_lot_id, mapping.service_code, price, "RUB"
    )
    return PriceObservationRecord(
        f"obs-{mapping.seller_id}-{sequence}", observation, mapping.material_snapshot_hash, "stable", sequence
    )


def _seller(seller_id: str, family: SellerFamily) -> TrustedSeller:
    return TrustedSeller(
        seller_id, f"Mock {seller_id}", family, True,
        SellerVerificationState.VERIFIED, SellerLastCheckedState.CURRENT,
    )


def _mapping(seller_id: str, service_code: str) -> CompetitorLotMapping:
    return CompetitorLotMapping(
        seller_id, f"lot-{seller_id}", service_code, MappingState.CONFIRMED, f"hash-{seller_id}"
    )


def _price_coordinator(
    database: Database,
    adapter: MockOwnLotPriceAdapter,
    *,
    service_code: str,
    prices: tuple[int, ...],
    history_prices: tuple[int, ...] = (),
    mode: OwnLotPricingMode = OwnLotPricingMode.AUTOMATIC,
    fixed: int | None = None,
    floor: int = 1_000,
) -> PriceUpdateCoordinator:
    seller_ids = tuple(f"seller-{index}" for index in range(len(prices)))
    sellers = tuple(_seller(item, SellerFamily.MYTHIC_PLUS) for item in seller_ids)
    mappings = tuple(_mapping(item, service_code) for item in seller_ids)
    records = tuple(
        PriceObservationRecord(
            f"obs-{item}-3", TrustedPriceObservation(item, f"lot-{item}", service_code, price, "RUB"),
            f"hash-{item}", "stable", 3,
        )
        for item, price in zip(seller_ids, prices)
    )
    if len(seller_ids) == 1:
        history = tuple(
            PriceObservationRecord(
                f"history-{seller_ids[0]}-{sequence}",
                TrustedPriceObservation(
                    seller_ids[0], f"lot-{seller_ids[0]}", service_code, price, "RUB"
                ),
                f"hash-{seller_ids[0]}", "stable", sequence,
            )
            for sequence, price in enumerate(history_prices, 1)
        )
    else:
        history = tuple(
            PriceObservationRecord(
                f"history-{item}-1",
                TrustedPriceObservation(item, f"lot-{item}", service_code, price, "RUB"),
                f"hash-{item}", "stable", 1,
            )
            for item, price in zip(seller_ids, history_prices)
        )
    lot_id = next(iter(adapter.prices))
    return PriceUpdateCoordinator(
        observation_adapter=MockCompetitorObservationAdapter(records),
        own_price_adapter=adapter,
        safety_engine=SafetyValidatedPricingEngine(),
        snapshots=PriceSnapshotRepository(database),
        lots=(ManagedPriceLot(
            lot_id, SellerFamily.MYTHIC_PLUS,
            OwnLotPriceState(service_code, adapter.prices[lot_id], "RUB", mode, fixed),
        ),),
        sellers=sellers,
        mappings=mappings,
        history=history,
        policies={service_code: PricePolicy(floor, 100, "RUB")},
    )


def _two_family_coordinator(database: Database, adapter: MockOwnLotPriceAdapter) -> PriceUpdateCoordinator:
    services = (("mplus", SellerFamily.MYTHIC_PLUS, "own-m"), ("delves", SellerFamily.DELVES, "own-d"))
    sellers = tuple(
        _seller(f"{code}-{index}", family)
        for code, family, _ in services for index in range(2)
    )
    mappings = tuple(_mapping(seller.seller_id, seller.seller_id.rsplit("-", 1)[0]) for seller in sellers)
    records = tuple(
        PriceObservationRecord(
            f"obs-{seller.seller_id}",
            TrustedPriceObservation(
                seller.seller_id, f"lot-{seller.seller_id}", seller.seller_id.rsplit("-", 1)[0],
                10_000, "RUB",
            ),
            f"hash-{seller.seller_id}", "stable", 2,
        )
        for seller in sellers
    )
    return PriceUpdateCoordinator(
        observation_adapter=MockCompetitorObservationAdapter(records),
        own_price_adapter=adapter,
        safety_engine=SafetyValidatedPricingEngine(),
        snapshots=PriceSnapshotRepository(database),
        lots=tuple(ManagedPriceLot(
            lot_id, family, OwnLotPriceState(code, 11_000, "RUB", OwnLotPricingMode.AUTOMATIC)
        ) for code, family, lot_id in services),
        sellers=sellers,
        mappings=mappings,
        history=(),
        policies={code: PricePolicy(1_000, 100, "RUB") for code, _, _ in services},
    )


def _desired_lot(code: str, *, desired_state: DesiredState = DesiredState.ENABLED) -> DesiredLotState:
    service = _catalog_service(code, CatalogFamily.MYTHIC_PLUS)
    service = CatalogService(
        service.stable_code, service.family, service.variant, service.enabled, desired_state,
        service.template_reference, service.description_profile, service.price_policy_reference,
        service.price_conditions,
    )
    return DesiredLotState(
        service, "Desired title", DescriptionTarget("Desired description", DescriptionConfirmation.CONFIRMED),
        "node-1", {"level": "10"}, "policy",
    )


def _current_lot(
    lot_id: str,
    service_code: str | None,
    *,
    title: str = "Desired title",
    description: str = "Desired description",
    active: bool = True,
    fields: dict[str, str] | None = None,
) -> CurrentLotState:
    return CurrentLotState(
        lot_id, service_code, title, description, 10_000, active, "node-1",
        fields or {"level": "10"}, {"timed": "yes"},
    )
