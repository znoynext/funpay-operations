# funpay-operations

Windows background-application scaffold for controlled FunPay operations.
This project is proprietary, **not open source**, and is publicly readable only
for transparency.

Copyright (c) 2026 znoynext. **All Rights Reserved.**

Technical mock readiness is recorded in
[`TECHNICAL_READINESS.md`](TECHNICAL_READINESS.md) as
`TECHNICALLY_READY_FOR_CONNECTION`. This is not authorization for live writes
and does not indicate that any real FunPay or Telegram account is connected.

## What is included

- A Python package with a command-line entry point and an `asyncio` background runner.
- SQLite persistence for lots and message templates; no statistics subsystem.
- Versioned, transactional SQLite migrations for sellers, lots, prices,
  descriptions, local FunPay dialogs/messages, event de-duplication, task
  state, and rollback price snapshots.
- Separate modules for FunPay, Telegram, lots, pricing, messages, and tasks.
- Independent Mythic+ and Delves service models with stable internal codes,
  validation, serialization, and in-memory duplicate protection.
- A configurable local Service Catalog that expands future Mythic+ and Delves
  variants without creating FunPay lots.
- Versioned public YAML seasonal-data records and a fail-closed description
  preview generator for Mythic+ and Delves.
- Configuration from `.env` and YAML, with safe sample files only.
- A Windows DPAPI setup command for local secret storage. It never writes secrets to Git or `.env`.
- A native FunPay adapter with normalized profiles, lots, dialogs, and messages,
  backed by the pinned `fpx-engine` library rather than owner-supplied endpoints.
- A local-only Own Lot Registry which reads owned-lot editor snapshots without
  performing a FunPay mutation and classifies only unambiguous WoW services.
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

## Local Service Catalog

The catalog is a separate local planning layer for future services. It has no
FunPay or Telegram dependency and cannot create a lot. `catalog init-example`
copies a safe public fixture into ignored `data/service_catalog.json` and seeds
ignored `data/service_catalog.sqlite3`. Edit only that local JSON for your
ranges, regions, formats, package sizes, references, and price-affecting
condition values.

```powershell
funpay-operations catalog init-example
funpay-operations catalog validate
funpay-operations catalog preview
```

Mythic+ uses configurable `min_key_level`/`max_key_level`, regions,
self-play/pilot formats, and package sizes (which must include `x1`). Delves
uses independently configurable tier bounds, `normal`/`bountiful` modes,
regions, optional formats, and package sizes. No current game range is a
hard-coded catalog rule; only generic positive technical bounds are enforced.

Each generated SQLite record contains a stable code, family, variant,
enabled flag, desired state, template reference, description profile,
price-policy reference, and normalized price conditions. Stable codes are
deterministic: `mplus_k{level}_{region}_{format}_x{size}` and
`delve_t{tier}_{mode}_{region}_{format}_x{size}`, followed by sorted
`_{condition}_{value}` suffixes. Validation rejects reversed/invalid ranges,
missing `x1`, duplicate package sizes or choices, unsupported formats/modes,
condition names that conflict with variants, and duplicate stable codes.

## Local lot synchronization plan

`funpay-operations lots plan-sync` is a local dry-run planner. It reads the
local Own Lot Registry and local Service Catalog only; it does not load a
FunPay session, contact FunPay or Telegram, or call a lot-write method.

```powershell
funpay-operations lots plan-sync
```

It produces an in-memory `LotSyncPlan` with one action per stable service code.
An action records the current and desired safe summaries, changed fields,
required write capabilities, safety status, and a reason. Decisions are
`already_correct`, `create_required`, `update_required`, `disable_required`,
`enable_required`, `ambiguous`, `blocked`, or `unsupported`.

Lot identity is never inferred from title, description, or price. It needs a
local confirmed mapping in `lot_service_mappings` between the stable service
code and an Own Lot Registry ID. Multiple confirmed candidates are marked
`ambiguous`, so the planner neither picks one nor proposes a duplicate. A
missing mapping is the only case that can require creation, and only when every
existing lot already has a different confirmed mapping; an unmapped existing
lot makes the proposed creation `ambiguous`. Repeating the same dry-run plan
yields locally skipped actions and still makes no write calls.

Diffs cover title, description, price-policy placeholder, enabled state,
editor form fields, category/node, and price-affecting service conditions.
The price value is deliberately only a placeholder at this stage. A seasonal
description marked unconfirmed is blocked; it can proceed only when local
configuration supplies an explicit safe-neutral template.

## Trusted seller matching (mock-only)

`trusted_sellers` is a local technical engine with no FunPay connection and no
real seller fixtures. It stores mock seller profiles, manually confirmed
competitor-lot-to-service mappings, and only a hash of each lot's material
title/form-fields/options snapshot. Matching accepts a service automatically
only as an `exact` result: category, region, Mythic+ key level or Delves tier
and normal/Bountiful mode, service format, package size, and every substantial
condition must agree. Missing input is `insufficient_data`; no candidate is
`incompatible`; multiple complete candidates are `ambiguous` and are never
chosen.

The local `ManualSellerConfirmationAPI` adds mock sellers, enables an explicit
confirmation or remap only for an enabled verified seller and an exact result,
and supports disable/remove actions. A changed title, form field, or form
option invalidates its mapping and marks it `revalidation_required`; no
automatic remapping occurs.

## Local pricing engine

`PricingEngine` is network-independent and uses integer minor units only. For
an automatic lot its sole formula is `minimum_valid_trusted_price * 99 // 100`.
It then rounds down to the configured FunPay minor-unit price step and applies
an aligned hard floor. Only enabled, verified trusted sellers with a confirmed
exact mapping, a matching currency, and a positive integer observation count.
No market or unmapped seller input is used.

Own-lot modes are `automatic`, `fixed_price`, `paused`, and `check_only`.
Fixed lots use their local manual price; paused lots retain their current price;
check-only lots calculate a target but return no update action. With no valid
trusted observation the result is always `keep_current_price`. Batch previews
are deterministic and make no writes.

## Pricing safety and market consensus

`PriceObservationValidator` rejects observations with an unstable seller or
lot ID, disabled/unverified seller, non-confirmed or wrong-service mapping,
currency/price errors, material identity changes, or changed historical
structure. `MarketConsensusEngine` marks an isolated low observation as
`suspicious` and uses the next valid minimum instead. It accepts any size of
real price decline when at least two independent sellers have a documented
downward movement with unchanged lot identity; there is no maximum-drop cap.

One seller needs consecutive close local observations with the same identity
before its price is accepted. Safety outcomes are `valid`, `suspicious`,
`rejected`, and `awaiting_confirmation`. The batch guard blocks a configurable
number of simultaneous extreme downward targets that lack multi-seller
consensus; its threshold is a risk signal, never a cap on consensus-confirmed
volatility.

## Mock price transactions and rollback

`PriceUpdateCoordinator` is a technical mock-only transaction engine. For each
Mythic+ and Delves family independently it fetches normalized observations,
applies mapping/anomaly/consensus checks, calculates decisions, validates the
batch, snapshots current local prices, writes only different targets, rereads,
and verifies. A verification mismatch gets exactly one retry and reread; a
remaining mismatch fails the lot and marks that family `unsafe_for_raise` with
an error reason. `PriceSnapshotRepository` restores the latest completed
family snapshot through the same write/reread/verify path.

The safe CLI uses empty mock adapters only:

```powershell
funpay-operations prices check
funpay-operations prices dry-run-update
funpay-operations prices rollback-preview
```

## Mock raise coordination

`RaiseCoordinator` is mock-only. Each run first executes a fresh complete
price transaction: observation fetch, mapping/anomaly validation, pricing,
price write, reread, and verification. It then handles Mythic+ and Delves
independently: only a completed, `safe_for_raise` family can reach the narrow
raise capability interface. Unsupported, unavailable, and cooldown states are
recorded honestly without a raise call. The local attempt ledger stores the
last attempt/result, known `next_allowed_at`, and failure reason; deterministic
schedule keys prevent duplicate attempts. A configurable local cooldown is an
abstraction only and never claims to be a FunPay-provided limit.

The existing fpx raise operation is account-wide, rather than per-lot. No
production raise adapter is composed at this stage, and no real raise can be
performed by this coordinator.

## Background runtime infrastructure

The application uses an advisory OS singleton lock in `data/`, an async task
supervisor, per-task exponential backoff, reconnect hooks, heartbeats, and
graceful cancellation. The FunPay message poller, Telegram poller, price
scheduler, raise scheduler, and recovery coordinator are separate tasks.
Disabled adapters are a healthy state and require no secrets; only task
exceptions are retried with backoff.

After a sleep/resume or Windows network-recovered signal, recovery always runs
in this order: validate external sessions, catch up messages, refresh market,
recalculate, verify own prices, then restore raise scheduling. The current
hooks are intentionally disabled unless a later approved adapter is composed.

SQLite maintenance performs `PRAGMA integrity_check` and creates local SQLite
backups using the SQLite backup API. Backups remain under the ignored data
directory; `storage.backup_retention_count` (default `7`) bounds retention and
`storage.backup_interval_seconds` (default `3600`) controls the maintenance
task. Application logs use the existing rotating local handler.

## Telegram control layer

`TelegramControlRouter` is a compact, button-first dashboard tested only with
`MockTelegramApi` and `MockControlService`. It presents Mythic+, Delves,
prices, messages, sellers, lots, update+raise, and settings without exposing
internal service codes or FunPay IDs in normal screens. Inline navigation uses
Back and Home, and callback navigation edits the same Telegram message instead
of posting a new one where this is safe. Technical lot details are available
only under **Подробнее**.

Price updates, rollback, update+raise, lot sync, seller add/remove, and emergency
resume use a preview/confirmation/result flow. Price checks remain read-only
and do not request confirmation. Buttons carry only short opaque local tokens,
are bound to the allowlisted user and current screen, and stale buttons offer a
safe refresh rather than performing an action.

Emergency stop is persisted locally and blocks lot writes, price writes, raise,
auto-reply, automated outbound messages, and Telegram-to-FunPay replies. It
does not stop incoming notification polling. Allowlisted private-chat checks
remain mandatory for every control action.

## Windows standalone installation

Generic builds use PyInstaller plus a small internal .NET helper and produce four binaries: background
`dist/funpay-operations.exe` (no console), technical
`dist/funpay-operations-cli.exe`, and the normal local GUI
`dist/funpay-operations-setup.exe` (no console), and
`dist/funpay-operations-auth.exe` for the fixed FunPay login window.
`THIRD_PARTY_NOTICES.md` accompanies the helper. None contains user
configuration, DPAPI secrets, databases, logs, or customer data. CI builds all
four on `windows-latest`, installs them into a temporary per-user directory,
and executes only safe smoke checks.

Per-user files live below `%LOCALAPPDATA%\FunPay Operations`: application,
config, and separate `data` subdirectories for DPAPI secrets, SQLite, logs,
and backups. Updating the executable leaves these directories untouched;
uninstalling the executable also preserves them until the user deliberately
removes them.

For normal Windows use, open **FunPay Operations Setup** from the current
user's Start Menu. It points to the installed GUI and needs neither Python nor
PowerShell. The developer/managed install flow
`scripts/install_local_windows.ps1` builds the current checkout, verifies all
four outputs and the required notice, atomically updates `%LOCALAPPDATA%\FunPay Operations\app`,
creates the folders and SQLite database, applies migrations, repairs the
per-user autostart task, creates or updates the Start Menu shortcut, and opens
the Setup Center. It fails rather than reporting an installation if any final
binary is missing or empty.

The guided catalog screen lets an owner choose Mythic+, Delves, ranges, formats
and packages, shows the number of local services before saving, and stores the
validated result in local SQLite. The minimum-price screen records amounts in
rubles with the plain-language promise: *the bot will never set a price below
this value*. No YAML paths or internal identifiers are required in this flow.

`funpay-operations diagnostics` prints a short summary such as Application,
Database, Autostart, FunPay, Telegram, and Service catalog. Missing account
data is shown as **не настроен**, never as a crash. Technical failures use a
short retry/details state; the traceback is reserved for local logs.

The Task Scheduler task is `FunPay Operations Background`, scoped to the
current Windows user, delayed 30 seconds after logon, uses limited privileges,
and starts the noconsole executable. `install-autostart`, `remove-autostart`,
`show-autostart-status`, and `repair-autostart` remain available for technical
support. `funpay-operations uninstall` removes only the autostart task and
explicitly preserves the local data and encrypted secrets.

## Connection setup UX

The Setup Center's **Войти в FunPay** flow opens a dedicated, user-driven
Microsoft Edge WebView2 window. It uses its own short-lived profile below
`%LOCALAPPDATA%\FunPay Operations\data\auth-temp-*`, never reads Chrome or
Edge profile databases, and allows only HTTPS `funpay.com` navigation. The
user enters credentials, 2FA, and any CAPTCHA directly in that window; the app
does not fill forms, inject JavaScript, bypass checks, or control mouse and
keyboard.

The official Microsoft.Web.WebView2 SDK is pinned to `1.0.4129.50`; its
redistribution notice is included in [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
The helper asks WebView2 CookieManager only for `https://funpay.com/`, selects
only `golden_key` and `golden_seal`, protects the short-lived hand-off with the
current user's Windows DPAPI, and closes. The Python service performs the
production read-only authorization and profile checks before it stores the
session in the existing DPAPI SecretStore; it then reads it back and checks it
once more. The temporary profile is deleted after the hand-off and retried on a
later launch if Windows still has a file lock. Neither cookie appears in
command-line arguments, logs, configuration files, SQLite, Telegram, CI, or
artifacts.

Top-level navigation is limited to HTTPS FunPay plus the exact VK OAuth hosts
(`id.vk.com` and `oauth.vk.com`) needed for FunPay's documented VK sign-in;
other sites, downloads, pop-ups, external protocols, DevTools, and context
menus are blocked. CAPTCHA and 2FA stay entirely user-driven.

Manual session entry remains only under **Расширенные настройки** for emergency
recovery; it is never the normal user path and does not instruct ordinary users
to use DevTools or F12. Existing sessions can be read-only checked without
replacement; **Войти заново** asks for confirmation before replacing one.

The auth window requires the official Evergreen Microsoft Edge WebView2 Runtime.
If it is absent, Setup Center reports the requirement and does not fall back to
manual cookies or download a runtime from an untrusted source.

The Telegram screen keeps the Bot Token in DPAPI rather than YAML, validates it
with `getMe`, and displays only the bot username. After the owner presses
`/start`, Setup Center shows a masked ID and requires an explicit **Это я**
confirmation. Only then is that account saved in the local allowlist and chosen
as the notification user; the first sender is never accepted automatically.

If FunPay later rejects a configured session, the local session guard persists
the `expired` state, blocks outbound FunPay replies, auto-replies, price/lot
writes, and raises, and prevents an endless network retry loop. If Telegram
is configured, the user-facing notification is:

```text
🔴 Требуется повторная авторизация FunPay

Сессия больше не действует.
Автоматические изменения остановлены.
```

Its private, allowlisted Telegram actions are **Авторизоваться** and
**Статус**. The first action starts only the installed local Setup Center with
the fixed `--funpay-auth` argument; it never transfers login data over Telegram
and is rate limited. If no interactive Windows desktop is available, Telegram
reports that the window will be available after Windows sign-in. Replacing the
local DPAPI session marks a new reconnect attempt automatically; a successful
read clears the blocked state without reinstalling the application and sends a
safe confirmation to the confirmed owner when Telegram is configured.

The main Setup Center never displays YAML, JSON, SQLite paths, raw FunPay IDs,
or a traceback. **Подробнее** information is redacted before it is written to
the local setup diagnostics log. **Перезапустить бота** asks the cooperative
background runtime to shut down, waits for it, and starts only the installed
noconsole executable. It does not use Task Manager automation. All FunPay lot,
price, raise, reply, and automated-message writes remain disabled by the safe
configuration until a separate future authorization step.

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

## Developer-only local setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -e .
Copy-Item config.example.yaml config.yaml
Copy-Item .env.example .env
python -m funpay_operations --once
```

The commands below are retained only for technical recovery and automated tests.
Normal users should use the installed Setup Center instead. The legacy
Windows-only DPAPI wizard reads values without echoing and encrypts them for the
current Windows user.

```powershell
funpay-setup init
funpay-setup set telegram_bot_token
funpay-setup set-funpay-session
funpay-setup diagnostics
```

`set-funpay-session` prompts separately for `golden_key` and `golden_seal`, then
stores one JSON session value only in the current Windows user's DPAPI store.
It never prints either cookie. `config.yaml` contains only logical secret key
names, operating mode, and polling/reconnect intervals; Setup Center stores a
confirmed Telegram owner locally without requiring a manually entered numeric
ID.
Supported modes are `safe`, `dry_run`, and `live`; `safe` is the default and
does not permit real operations. Diagnostics report secret presence only as
`<masked>` or `<missing>`.

The generated `data/` directory, SQLite database, logs, and DPAPI store are
ignored by Git. Do not put tokens, session data, messages, customer data, or
transaction data in source files, YAML, or commits.

## Telegram control bot

The bot uses the official Telegram Bot API with long polling only; webhooks are
not configured. After future local setup, `/start` or `/status` opens the
dashboard; normal use is button-first and does not require remembering commands,
IDs, or service codes. The bot accepts controls only from an allowlisted
**private** chat; rejected requests are recorded in the local security log
without message text or token values. The token is never read by CI or stored in
Git.

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

## Lot write capabilities (technical layer only)

`funpay_operations.lot_writes` defines the separate `LotWriteClient` contract
and a native adapter for future lot changes. It recognizes the actual public
`fpx-engine` methods for price, short title, description, enable/disable,
creation, and account-wide raise. Generic field updates are marked unsupported:
the pinned library has no public generic editor method. Raise is explicitly
account-wide in fpx; it is not a targeted single-lot bump.

Each capability reports `supported`, `unsupported`, or
`unavailable_without_live_session`. Calls return a structured technical result:
`requested`, `skipped`, `unsupported`, `succeeded`, `failed`, or
`verification_required`. An in-memory `MockLotWriteClient` is used by CI.

This release does **not** enable a production write. In `safe` mode every call
is skipped. In `dry_run` it creates an in-memory adapter-operation plan without
sending anything. A real fpx form includes live, dynamic editor state, so the
plan intentionally represents the documented fpx method and validated
arguments rather than a guessed HTTP payload. In `live` mode the adapter is
architecturally present but returns `verification_required`: production network
mutation is hard-disabled pending a separate approved step. No real FunPay
session or write operation is needed for this layer.

Run the read-only local integration check after storing the session:

```powershell
funpay-operations smoke-test --config config.yaml
```

It verifies the local session format, authorization, profile, own-lot read,
latest-dialog read, and client closure. Its output contains only success states
and aggregate lot/dialog counts: no cookies, message text, buyer names, or full
external IDs.

## Own Lot Registry / discovery

`discover-lots` reads the authenticated seller's existing lots and stores the
result only in the ignored SQLite database. It uses `fpx-engine`'s read-only
editor snapshot to retain the category node, current price, public title and
description, location, explicit activity/deletion state, current non-sensitive
form fields, and the declared non-sensitive options for that category node.
This is discovery only: it does not create, edit,
enable, disable, reprice, or raise a lot.

```powershell
funpay-operations discover-lots --config config.yaml
```

The command prints just aggregate counts (`own_lots`, `mythic_plus`, `delves`,
and `unmapped`) and whether exemplars are selected; it never prints a title,
description, full lot ID, account ID, cookies, or form contents. The local
registry explicitly excludes CSRF values, auto-delivery secrets, and payment
messages even though those can occur in an editor form.

Mythic+ and Delves classification relies on explicit, unique markers in the
lot's own title/description. Mythic+ requires a key level, region,
self-play/pilot format, and `xN` package size to become mapped. Delves requires
an explicit tier, Normal/Bountiful state, region, format, and package size.
Anything unknown, mixed, incomplete, or contradictory remains `unmapped`; no
field is inferred from a price or an ambiguous phrase. The unstructured public
description/short-description remains available only in local SQLite as the
source for later human review of conditions.

To save one already mapped lot as a local exemplar, run discovery with a prompt
for the relevant existing ID; typed IDs are hidden and are not placed in shell
history:

```powershell
funpay-operations discover-lots --config config.yaml --select-mythic-template
funpay-operations discover-lots --config config.yaml --select-delves-template
```

An exemplar is only a local pointer to an existing, mapped lot. It makes no
request that changes FunPay and no edit feature is enabled by selecting it.

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
