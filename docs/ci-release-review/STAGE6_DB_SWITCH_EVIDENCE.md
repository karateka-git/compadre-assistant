# Шаг 6.1: протокол локального опыта

Проверка от 03.11.2026 относится к выбору механизма в
[решении о переключении SQLite](STAGE6_DB_SWITCH.md). Production-код, VPS,
настоящие пользовательские БД и действующие контейнеры не изменялись.
Независимое чтение решения и стенда родительским агентом не выявило
дополнительных замечаний; это review проекта протокола, не эксплуатационный допуск.

## Окружение и запуск

Worktree: `C:/Users/compadre/.codex/worktrees/ci-release-review-plan/obsChatBot`,
ветка `codex/ci-release-review-plan`. В worktree `.venv` отсутствует;
проверены наличие и stdlib-зависимости основного проектного окружения.
Python 3.12.10, SQLite 3.49.1, Windows (`os=nt`), Docker Compose v5.5.1.

Команда из корня worktree:

```powershell
$env:PYTHONUTF8='1'
& 'C:/Users/compadre/Documents/obsChatBot/.venv/Scripts/python.exe' docs/ci-release-review/sqlite_switch_experiment.py
```

Сохранённый stdout/stderr последнего полного прогона:

```text
Python 3.12.10; SQLite 3.49.1; os=nt
test_ambiguous_state_rejected (__main__.SwitchExperiment.test_ambiguous_state_rejected)
Отклоняет чужой selector, неверную границу и отсутствующую SQLite. ... ok
test_compose_config (__main__.SwitchExperiment.test_compose_config)
Проверяет настоящий Compose config без daemon и без секретов проекта. ... ok
test_crash_windows (__main__.SwitchExperiment.test_crash_windows)
Прерывает 12 границ; сохраняет WAL и повторяемый выбор recovery. ... ok

----------------------------------------------------------------------
Ran 3 tests in 12.878s

OK
```

Три unittest включают 12 subtests аварийных окон и 6 subtests отказа;
это не 21 независимый test method. Каждый сценарий использует собственный
TemporaryDirectory. Повторяемый [стенд](sqlite_switch_experiment.py) содержит
все assertions; временные файлы после проверки не являются артефактом.
Предыдущий прогон без Compose-проверки также прошёл: 2 tests, 11.642s.

## Наблюдаемые результаты

| Точка аварии | Selector после process crash | Выбор recovery |
| --- | --- | --- |
| `intent`, `selector-written`, `selector-synced` | old | old |
| `selector-replaced`, `selector-durable`, `selected` | new | old |
| `admission-written`, `admission-synced` | new; старый state с false | old |
| `admission-replaced`, `admission-durable`, `before-polling` | new; state с true | new |
| `after-write` | new; state с true; WAL содержит новую строку | new, новая строка сохранена |

Дочерний процесс завершается через `os._exit(73)` без cleanup; контролирующий
процесс проверяет код завершения. Коммиты реально идут в WAL. До перехода
у old и new имеются все шесть файлов; все их SHA256 совпадают после
переключения во всех окнах без дополнительной записи. Чтение каждого
поколения с `integrity_check` после аварии возвращает только ожидаемые строки:
old — исходную и old-only, new — исходную и candidate-only, после границы
также after-admission. Snapshot содержит исходную committed строку из WAL.

Чужой transition, неизвестное имя, true при selector old, отсутствие выбранной
БД, испорченный JSON и отсутствие boolean-границы дают отказ resolver.
Повтор resolver без изменения файлов даёт тот же результат. Это проверка
идемпотентного **решения**, не тест повторного запуска production recovery.

Настоящий `docker compose config --format json` обработал копию текущего
`docker-compose.yml` с синтетической `.env` и экспериментальным override.
Оба сервиса получили `/app/data/app-generation-test.db`, несмотря на
legacy `DATABASE_PATH=/app/data/app.db` в env_file; каждый сохранил один
bind mount на `/app/data`. Без `ASSISTANT_DATABASE_PATH` Compose отказал.
Проектная `.env` не читалась и её значения не попадали в вывод.

## Что опыт не доказывает

Локальный `docker info --format '{{.OSType}}'` сообщил:

```text
failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine;
check if the path is correct and if the daemon is running:
open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified.
```

Daemon не запускался. Реальный Linux bind mount, контейнеры, systemd,
старый image startup с runtime admission, backup timer и power loss
**не проверены**. Windows не исполняет POSIX `fsync(directory)` ветку стенда;
название crash point `durable` обозначает место в прототипе, а не доказанную
устойчивость Windows storage к отключению питания. Сценарии после replace
проверяют process crash; для reboot до directory fsync возможны обе версии
selector/state, как определено в решении.

В стенде используется синтетическая таблица вместо миграций проекта;
настоящая миграция окончательного снимка относится к 6.2. Не моделируются
права root, подмена inode/alias, fsync failure, disk full, полный отказ
storage и параллельный writer. Их проверки, полноценный recovery и
эксплуатационная приёмка остаются 6.2–6.6 и этапом 7.

Шаг 6.1 завершён как выбор и локальная проверка механизма с зафиксированными
границами доказательства. Следующий шаг — 6.2; это не завершение этапа 6.
