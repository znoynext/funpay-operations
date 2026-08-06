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
