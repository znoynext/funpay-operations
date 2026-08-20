"""Read-only, privacy-preserving checks for the local FunPay adapter."""

from __future__ import annotations

from typing import TextIO

from .database import Database
from .funpay import FunPayError, NativeFunPayClient
from .lot_discovery import OwnLotRegistryRepository, RegisteredLot, classify_wow_lot


def run_smoke_test(client: NativeFunPayClient, *, output: TextIO, database: Database | None = None) -> int:
    """Exercise the native integration without printing account or buyer data."""

    if not client.has_local_session():
        print("smoke-test: local_session=missing_or_invalid", file=output)
        return 1
    try:
        if not client.check_authorization():
            print("smoke-test: authorization=failed", file=output)
            return 1
        client.get_profile()
        lots = client.get_own_lot_details()
        dialogs = client.get_dialogs()
    except FunPayError as error:
        print(f"smoke-test: failed={error.__class__.__name__}", file=output)
        return 1
    finally:
        client.close()
    registered = tuple(RegisteredLot(item, classify_wow_lot(item)) for item in lots)
    managed_ids: set[str] = set()
    if database is not None:
        database.initialize()
        OwnLotRegistryRepository(database).replace(registered)
        with database.session() as connection:
            rows = connection.execute(
                """SELECT registry.external_id FROM own_lot_registry registry
                JOIN lot_service_mappings mappings ON mappings.external_lot_id = registry.external_id
                JOIN service_catalog catalog ON catalog.stable_code = mappings.service_code
                WHERE registry.classification = 'mythic_plus' AND registry.mapping_state = 'mapped'
                  AND catalog.family = 'mythic_plus'"""
            ).fetchall()
        managed_ids = {row["external_id"] for row in rows}
    ambiguous = sum(item.classification.ambiguous for item in registered)
    print(
        "smoke-test: local_session=present authorization=ok profile=ok "
        f"own_lots_total={len(lots)} managed_mythic_plus={len(managed_ids)} "
        f"unknown_non_managed={len(lots) - len(managed_ids) - ambiguous} ambiguous={ambiguous} "
        f"dialogs={len(dialogs)} closed=ok",
        file=output,
    )
    return 0
