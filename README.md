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
- Configuration from `.env` and YAML, with safe sample files only.
- A Windows DPAPI setup command for local secret storage. It never writes secrets to Git or `.env`.
- A read-only FunPay client boundary with normalized profiles, lots, dialogs, and
  messages. It uses only authenticated `GET` requests, retries transient network
  failures with a bound, and enforces a per-client request interval.
- Rotating local logs under the ignored `data/` directory.

The scaffold deliberately contains no browser automation, mouse/keyboard control,
visible browser, or implementation of real FunPay actions. Real operations are
disabled by default and require explicit owner approval for each concrete action.

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
funpay-setup set funpay_session
funpay-setup diagnostics
```

`config.yaml` contains only logical secret key names, allowlisted Telegram user
IDs, operating mode, polling/reconnect intervals, and relative local paths.
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

To enable notifications, configure an owner-verified `new_messages` endpoint,
set `funpay.message_notifications_enabled: true`, and set
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
`live` mode and has an owner-verified `funpay.reply_endpoint` that honours the
idempotency key; CI uses no such configuration or session.

## FunPay read integration

FunPay does not provide a documented, stable public seller API contract. The
project therefore does not ship guessed endpoints or a browser automation
workaround. Configure only owner-verified **relative** GET paths under
`funpay.read_endpoints` in the ignored local `config.yaml`; the supported
placeholder names are `{seller_id}`, `{after_message_id}`, and `{lot_id}`.
Until a path is configured, that capability fails closed. The adapter accepts a
session only through a callable backed by the ignored Windows DPAPI store and
does not log, print, commit, or send it to CI. It rejects HTTP 401/403 as an
expired session and represents unavailable networking separately from malformed
responses. No endpoint in this release can create, edit, delete, send, or bump
anything on FunPay.

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
