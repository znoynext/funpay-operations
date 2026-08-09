# funpay-operations

Windows background-application scaffold for controlled FunPay operations.
This project is proprietary, **not open source**, and is publicly readable only
for transparency.

Copyright (c) 2026 znoynext. **All Rights Reserved.**

## What is included

- A Python package with a command-line entry point and an `asyncio` background runner.
- SQLite persistence for lots and message templates; no statistics subsystem.
- Versioned, transactional SQLite migrations for sellers, lots, prices,
  descriptions, local FunPay dialogs/messages, event de-duplication, task
  state, and rollback price snapshots.
- Separate modules for FunPay, Telegram, lots, pricing, messages, and tasks.
- Independent Mythic+ and Delves service models with stable internal codes,
  validation, serialization, and in-memory duplicate protection.
- Versioned public YAML seasonal-data records and a fail-closed description
  preview generator for Mythic+ and Delves.
- Configuration from `.env` and YAML, with safe sample files only.
- A Windows DPAPI setup command for local secret storage. It never writes secrets to Git or `.env`.
- A native FunPay adapter with normalized profiles, lots, dialogs, and messages,
  backed by the pinned `fpx-engine` library rather than owner-supplied endpoints.
- Rotating local logs under the ignored `data/` directory.

The scaffold deliberately contains no browser automation, mouse/keyboard control,
visible browser, or implementation of real FunPay actions. Real operations are
disabled by default and require explicit owner approval for each concrete action.

## Service models

`funpay_operations.services` defines only local domain descriptions; it neither
creates nor updates FunPay lots. Mythic+ records validate key level, region,
service format, package size, and normalized price conditions. Delves records
validate tier, Bountiful status, region, service format, package size, and
normalized price conditions. Codes are deterministic (for example, `mplus_10_selfplay_x1` and
`delve_t8_bountiful_selfplay_x1`); the deduplication key also includes regional
and price-condition variants where relevant.

## Seasonal data and description previews

Public seasonal metadata lives under `seasonal_data/v1/` and includes only
season, region, dates, reward ilvls, crests, verification date, sources, and
confirmation status. The published starter records deliberately remain
`unconfirmed` until an owner enters current, verifiable HTTPS sources and marks
them `confirmed`; the generator rejects every unconfirmed record. It does not
fetch source URLs, create FunPay lots, or use account data.

`DescriptionGenerator` produces previews only. Mythic+ through +12 emphasizes
verified rewards, ilvl, and crests; +13 and higher emphasizes Mythic+ rating and
high keys. Delves has its own template. Every template explicitly states that a
random item is not guaranteed.

Generate a local-only preview without creating or updating a FunPay lot:

```powershell
funpay-operations --preview-seasonal-data seasonal_data/v1/mythic_plus.yaml --preview-key-level 10
```

## Local setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -e .
Copy-Item config.example.yaml config.yaml
Copy-Item .env.example .env
python -m funpay_operations --once
```

To store a local secret, use the Windows-only setup wizard; it reads the value
without echoing it and encrypts it with DPAPI for the current Windows user.

```powershell
funpay-setup init
funpay-setup set telegram_bot_token
funpay-setup set-funpay-session
funpay-setup diagnostics
```

`set-funpay-session` prompts separately for `golden_key` and `golden_seal`, then
stores one JSON session value only in the current Windows user's DPAPI store.
It never prints either cookie. `config.yaml` contains only logical secret key
names, allowlisted Telegram user IDs, operating mode, and polling/reconnect
intervals.
Supported modes are `safe`, `dry_run`, and `live`; `safe` is the default and
does not permit real operations. Diagnostics report secret presence only as
`<masked>` or `<missing>`.

The generated `data/` directory, SQLite database, logs, and DPAPI store are
ignored by Git. Do not put tokens, session data, messages, customer data, or
transaction data in source files, YAML, or commits.

## Telegram control bot

The bot uses the official Telegram Bot API with long polling only; webhooks are
not configured. First store the token locally with `funpay-setup set
telegram_bot_token`, add your numeric Telegram user ID to the ignored
`config.yaml`, then set `telegram.enabled: true`. The bot accepts commands only
from an allowlisted **private** chat; rejected commands are recorded in the
local security log without message text or token values. `/start`, `/status`,
`/pause`, `/resume`, and `/stop` are available now, including a button menu.
The remaining menu commands reply that their corresponding FunPay module is not
yet available. The token is never read by CI or stored in Git.

## New FunPay-message notifications

To enable notifications, store the local DPAPI FunPay session, set
`funpay.message_notifications_enabled: true`, and set
`telegram.notification_user_id` to one of the allowlisted IDs in ignored local
`config.yaml`. Each locally stored incoming message is linked to the resulting
Telegram message and dialog before its cursor advances. Outgoing FunPay messages
are not notified. Message content is stored only in the ignored SQLite database,
never in repository files, CI logs, or artifacts.

## Buyer replies from Telegram

Reply directly to a notification or choose its **Reply** button and send the
next private Telegram message within five minutes. The application resolves the
dialog only from its local notification link, checks the recorded buyer, and
records an idempotency key before a send. On failure it exposes Retry and Cancel
buttons. Actual FunPay sends remain disabled until the local configuration is in
`live` mode with `operations.enabled: true`; CI uses neither a session nor a
network connection to FunPay. FunPay does not document server-side idempotency
for sends. The application keeps local durable reply-attempt keys to avoid
duplicate processing of the same Telegram update, but a retry after an unknown
network outcome can still be at-least-once.

## Automatic first reply

The automatic greeting is disabled by default. In a locally configured `live`
mode it can be toggled by an allowlisted owner with `/auto_reply_on` and
`/auto_reply_off`. When enabled, it sends the exact text `Привет` at most once
for the first newly observed buyer message in each dialog. The startup
synchronization is deliberately treated as history, so existing dialogs never
receive a greeting. Owner messages and duplicate or later incoming events never
trigger another greeting, including after periods of inactivity or an
application restart. Telegram notification delivery precedes the automatic reply
and remains available even if that reply fails.

## FunPay integration

FunPay does not publish a documented public seller API. This project therefore
uses the pinned [`fpx-engine` 0.7.4](https://pypi.org/project/fpx-engine/0.7.4/)
adapter (MIT) behind its own `FunPayClient`; it does not contain guessed endpoint
paths or a browser workaround. The pinned release maps to
[`funpayx/fpx` commit `34871cc14851`](https://github.com/funpayx/fpx/commit/34871cc14851)
and uses FunPay's `golden_key` plus `golden_seal` cookies. The adapter performs
authenticated profile, own/other seller lots, dialogs, message polling, and
buyer-message sends only. It reads a session only through the ignored Windows
DPAPI store and never logs, prints, commits, sends it to Telegram, or exposes it
to CI.

`fpx-engine` provides polling rather than a FunPay webhook/event listener. The
adapter polls chats at the configured message interval, asks each changed dialog
for messages newer than the durable local cursor, normalizes only valid
non-system events, and lets SQLite de-duplicate persistent delivery. That makes
reconnect recovery safe without inventing an event contract. A future bump step
can read each owned lot's category-node metadata, but this version never calls
the library methods for pricing, creating, changing, enabling, disabling, or
raising lots.

Run the read-only local integration check after storing the session:

```powershell
funpay-operations smoke-test --config config.yaml
```

It verifies the local session format, authorization, profile, own-lot read,
latest-dialog read, and client closure. Its output contains only success states
and aggregate lot/dialog counts: no cookies, message text, buyer names, or full
external IDs.

Migrations run automatically at startup. They are idempotent, applied inside a
transaction, and preserve the initial scaffold tables for compatibility. A
failed transaction rolls back rather than leaving partial task, event, message,
or price updates behind.

## Checks

```powershell
python -m pip install -e .
python -m unittest discover -s tests -v
python -m funpay_operations --once
```

Changes are validated by the `Repository policy` GitHub Actions workflow.
