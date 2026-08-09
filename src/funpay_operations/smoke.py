"""Read-only, privacy-preserving checks for the local FunPay adapter."""

from __future__ import annotations

from typing import TextIO

from .funpay import FunPayError, NativeFunPayClient


def run_smoke_test(client: NativeFunPayClient, *, output: TextIO) -> int:
    """Exercise the native integration without printing account or buyer data."""

    if not client.has_local_session():
        print("smoke-test: local_session=missing_or_invalid", file=output)
        return 1
    try:
        if not client.check_authorization():
            print("smoke-test: authorization=failed", file=output)
            return 1
        client.get_profile()
        lots = client.get_own_lots()
        dialogs = client.get_dialogs()
    except FunPayError as error:
        print(f"smoke-test: failed={error.__class__.__name__}", file=output)
        return 1
    finally:
        client.close()
    print(f"smoke-test: local_session=present authorization=ok profile=ok own_lots={len(lots)} dialogs={len(dialogs)} closed=ok", file=output)
    return 0
