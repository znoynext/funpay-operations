# TECHNICALLY_READY_FOR_CONNECTION

Verified on 2026-08-09 against mock adapters only.

This status means that the local technical infrastructure is ready for a
separately approved connection step. It does **not** mean that a real FunPay
account, Telegram bot, trusted seller, competitor mapping, or live mutation has
been configured or tested. Production FunPay lot, price, and raise execution
remains hard-disabled.

## Verified mock paths

- Messages: polling, normalization, SQLite persistence, FunPay-event and
  Telegram-update deduplication, notification linking, two-dialog reply routing,
  retry/failure handling, reconnect catch-up, emergency stop, and the exact
  one-per-dialog `Привет` greeting persisted across restart.
- Pricing: trusted seller repositories, exact mappings, validation, anomaly and
  consensus decisions, 99% integer-minor-unit pricing, rounding, hard floors,
  all four own-lot modes, snapshots, writes, read-back verification, one retry,
  rollback, independent family failure, and capability-gated raise.
- Lots: catalog-to-registry planning, duplicate/ambiguity guards, capability
  detection, mock writes, read-back replanning, verification, and a second
  idempotent synchronization cycle.
- Runtime: singleton lock, task isolation, exponential retry, session failure,
  network/timeouts, sleep/resume recovery order, graceful shutdown, SQLite
  integrity, backup creation, and retention.
- Telegram control: every menu entry through `MockTelegramApi`, confirmation
  callbacks, private allowlist enforcement, and an emergency barrier that blocks
  outbound mutations while leaving incoming notification reads available.
- Windows: clean/repeat/update-safe first run, safe config generation, packaged
  background startup without secrets, Task Scheduler command generation,
  autostart removal without data deletion, diagnostics, singleton behavior, and
  both PyInstaller executables.

## Capability boundary

The pinned fpx adapter can identify public library methods for price, short
title, description, enable, disable, creation, and account-wide raise. The
production adapter exposes these only through safe/dry-run planning and still
blocks live network mutation. Generic arbitrary field updates are unsupported,
and fpx has no per-lot raise capability.

## Security and dependency evidence

- Current tracked files and every reachable Git commit were scanned without
  finding credential-shaped FunPay/Telegram values or tracked `.env`, personal
  config, SQLite, log, or backup paths.
- Generic executable archives contain no config, `.env`, SQLite, log, or backup
  entries. Local ignored user data is never staged or uploaded.
- `fpx-engine==0.7.4` is pinned to PyPI provenance commit
  `34871cc148511c33867e1dc93e4ba43ab3061dbe` and is MIT licensed.
- Runtime requirements and transitives returned no known vulnerabilities from
  both the PyPI advisory service and OSV using `pip-audit==2.10.1`.
- Runtime dependencies use compatible licenses (MIT, BSD, Apache-2.0,
  MPL-2.0, PSF-2.0);
  PyInstaller is build-only and carries its exception for distributing non-free
  programs.

## Remaining connection risks

- FunPay has no documented public seller API, so a future live connection can
  reveal upstream behavior changes that mocks cannot prove.
- No real FunPay session, Telegram token, seller observation, message, or Task
  Scheduler installation was used in this readiness step.
- FunPay messaging does not provide claimed server-side idempotency; an unknown
  network outcome during a later live retry can remain at-least-once.
- Live lot/price/raise execution requires a separate explicit authorization,
  a real read-only smoke test first, and new regression evidence for any response
  shape that differs from the mock contract.
