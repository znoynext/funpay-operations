# FunPay Operations for World of Warcraft Mythic+

Локальное Windows-приложение для безопасной автоматизации существующих услуг
World of Warcraft Mythic+ на FunPay. Код публичен, но распространяется по
модели All Rights Reserved.

## Текущее состояние

Проект находится в фазе `TECHNICALLY_READY_FOR_CONNECTION` и работает в
production только в read-only режиме. Реальная FunPay-сессия и Telegram могут
быть настроены локально, но live writes не разрешены:

- чтение профиля, собственных лотов, публичных лотов доверенных продавцов,
  диалогов и новых сообщений;
- локальное обнаружение и строгая классификация собственных Mythic+ лотов;
- read-only расчёт цен по подтверждённым соответствиям;
- Telegram-уведомления и навигация по локальным данным;
- mock-only синхронизация лотов, price transactions, rollback и raise;
- Windows background runtime, singleton lock, recovery, backups и autostart.

Не выполняются реальные отправка сообщений, автоответ, изменение/создание/
включение/отключение лотов, изменение цен и raise. Ручное включение этих
операций через конфигурацию невозможно в текущем релизе.

### Локальная read-only проверка FunPay

Кнопка `🔍 Проверить FunPay` доступна в Setup Center и Telegram владельца.
Она записывает в SQLite только локальную команду; установленный background
process под тем же Windows user сам читает DPAPI и выполняет один ограниченный
цикл: profile/authorization, own lots и dialogs metadata. UI не получает
decrypted session и показывает только агрегаты без ID, имён покупателей и
текстов сообщений. Повторный параллельный запуск и частый спам блокируются.

Результат хранится в `read_only_probe_state`; реальные lot IDs остаются только
в предназначенном для них локальном Own Lot Registry. Проверка не запускает
market scan и не открывает ни одной write capability. После неё price writes,
lot writes, raise, auto-reply, Telegram replies и automation остаются
`DISABLED`.

## Граница управления Mythic+

Единственное управляемое семейство — `mythic_plus`. Собственный лот может
попасть в расчёт или будущую mutation pipeline только при одновременном
выполнении условий:

```text
managed_service_family == MYTHIC_PLUS
and identity_confirmed == true
and local mapping points to an active Mythic+ service code
```

Лоты без полного набора однозначных признаков отображаются как
«Не управляется ботом». Они не получают управляющих Telegram-кнопок, не входят
в pricing/sync и не передаются production write adapter. Account-wide raise из
`fpx-engine` помечен `unsupported`, поскольку он не обеспечивает эту границу.

Классификатор не угадывает услугу. Для управляемого Mythic+ лота нужны явные и
единственные значения key level, region, self-play/pilot и package `xN`, а
также подтверждённое локальное соответствие stable service code.

## Service Catalog

Пользовательский catalog хранится только локально в SQLite. Публичный fixture
[`examples/service_catalog.example.json`](examples/service_catalog.example.json)
содержит безопасные Mythic+ примеры с настраиваемыми диапазонами ключей,
регионами, форматами, package sizes и дополнительными price conditions.

Stable code имеет форму:

```text
mplus_k{level}_{region}_{format}_x{package}[_{sorted_conditions}]
```

Команды:

```powershell
funpay-operations catalog init-example
funpay-operations catalog validate
funpay-operations catalog preview
funpay-operations lots plan-sync
```

`plan-sync` только строит локальный dry-run план. Создание дублей блокируется,
если хотя бы один потенциальный существующий лот не имеет подтверждённой
Mythic+ identity.

## Own Lot Registry

Read-only discovery сохраняет локально ID, node/category, title, price,
activity, region, доступные editor fields/options, description metadata и
результат классификации. Чувствительные editor fields исключаются адаптером.

```powershell
funpay-operations discover-lots --config config.yaml
funpay-operations discover-lots --config config.yaml --select-mythic-template
```

CLI выводит только безопасные агрегаты: общее число лотов, управляемые Mythic+,
unknown/non-managed и ambiguous. Внешние ID, покупатели и тексты сообщений не
печатаются.

## Trusted sellers и pricing

Trusted seller автоматически относится к Mythic+. В production UI нет выбора
семейства. Competitor mapping принимается только при exact match категории,
region, key level, service format, package size и существенных условий. Любое
материальное изменение title/form/options требует повторного подтверждения.

Деньги представлены integer minor units; `float` не используется. Основная
формула:

```text
target = floor_to_step(minimum_valid_trusted_price × 0.99)
final_target = max(target, configured_minimum_price)
```

В расчёт входят только enabled/verified sellers, confirmed exact mappings и
валидные observations. Один подозрительно низкий outlier исключается; быстрое
общее снижение принимается только при объяснимом consensus. При отсутствии
валидного ориентира текущая цена сохраняется. Режимы `fixed_price`, `paused` и
`check_only` соблюдаются. Production показывает решение, но не записывает его.

## Сообщения и Telegram

Message pipeline не зависит от типа услуги:

```text
FunPay polling -> normalization -> SQLite -> deduplication
-> Telegram notification -> locked reply routing
```

История существующего диалога при первом запуске bootstrap-ится без массовых
уведомлений. После reconnect выполняется catch-up; duplicate events и duplicate
Telegram updates подавляются локально. Покупатели, полные ID и тексты сообщений
не попадают в обычные логи.

Автоответ содержит строго `Привет`, но остаётся выключенным. Emergency stop
продолжает блокировать все outbound mutations и automated messages, не
останавливая incoming notification pipeline.

## FunPay adapter и capabilities

Интеграция скрыта за собственными `FunPayClient` и `LotWriteClient`. Production
read adapter использует `fpx-engine==0.7.4` (MIT); ручные `read_endpoints` и
`reply_endpoint` не нужны.

Технически обнаруживаются:

- `update_price`, `update_title`, `update_description`, `enable_lot`,
  `disable_lot`, `create_lot` — доступны в библиотеке, но network execution
  отключён и требует подтверждённого Mythic+ guard;
- `update_fields` — `unsupported`, потому что библиотека не предоставляет
  публичную generic operation;
- `bump_raise` — `unsupported` в production, потому что библиотека поднимает
  категории account-wide, а не подтверждённый отдельный Mythic+ лот.

`safe` всегда пропускает writes, `dry_run` строит operation без отправки,
`live` архитектурно существует, но production factory жёстко задаёт
`live_execution_enabled=False`.

## Локальные секреты и данные

`golden_key`, `golden_seal` и Telegram token хранятся только локально через
Windows DPAPI. Они не записываются в YAML/SQLite, не логируются, не выводятся в
Telegram и не передаются GitHub Actions.

Стандартные Windows paths разделяют приложение и пользовательские данные:

```text
%LOCALAPPDATA%\FunPay Operations\app
%LOCALAPPDATA%\FunPay Operations\config
%LOCALAPPDATA%\FunPay Operations\data
%LOCALAPPDATA%\FunPay Operations\logs
%LOCALAPPDATA%\FunPay Operations\backups
```

Перед применением миграций существующая SQLite база проходит
`PRAGMA integrity_check` и получает локальную backup-копию; после миграции
integrity проверяется повторно. Legacy строки неподдерживаемых семейств не
удаляются, а исключаются из production repositories.

## Windows установка

Сборка PyInstaller создаёт три generic executable без пользовательских данных:

```text
dist\funpay-operations.exe          # background, no console
dist\funpay-operations-cli.exe      # CLI/debug
dist\funpay-operations-setup.exe    # setup center
```

Команды:

```powershell
# Из checkout: собрать, установить/обновить, проверить и открыть Setup Center
powershell -ExecutionPolicy Bypass -File scripts\install_local_windows.ps1

# После установки
& "$env:LOCALAPPDATA\FunPay Operations\app\funpay-operations-cli.exe" diagnostics
& "$env:LOCALAPPDATA\FunPay Operations\app\funpay-operations-cli.exe" install-autostart
& "$env:LOCALAPPDATA\FunPay Operations\app\funpay-operations-cli.exe" show-autostart-status
& "$env:LOCALAPPDATA\FunPay Operations\app\funpay-operations-cli.exe" repair-autostart
& "$env:LOCALAPPDATA\FunPay Operations\app\funpay-operations-cli.exe" remove-autostart
& "$env:LOCALAPPDATA\FunPay Operations\app\funpay-operations-cli.exe" uninstall
```

Task Scheduler запускает background executable для текущего пользователя после
login с задержкой. Singleton process lock не допускает второй экземпляр.
Uninstall удаляет autostart, но сохраняет локальные data/secrets.

## Безопасная диагностика

```powershell
funpay-operations diagnostics
funpay-operations smoke-test --config config.yaml
```

`diagnostics` рассматривает отсутствующие внешние secrets как
`not_configured`. `smoke-test` только читает авторизацию, профиль, собственные
лоты и последние диалоги, затем закрывает клиент; session cookies, полные ID,
покупатели и тексты сообщений не выводятся.

## Разработка и проверки

`PYTHONPATH` должен указывать на текущий `src`, чтобы не проверить случайно
старую установленную копию пакета:

```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
python -m unittest discover -s tests -v
python -m compileall -q src tests
python -m ruff check src tests
python -m pip check
python -m pip_audit -r requirements.txt
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
```

CI использует только mocks и generic fixtures, не подключается к FunPay или
Telegram и не получает локальный DPAPI store.

## License

Copyright (c) 2026 znoynext. All Rights Reserved.

No permission is granted to use, copy, modify, merge, publish, distribute,
sublicense, or sell copies of this software without prior written permission
from the copyright holder.
