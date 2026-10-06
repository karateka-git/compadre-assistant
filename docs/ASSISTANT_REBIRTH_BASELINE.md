# Исходное состояние проверок перед ASSISTANT REBIRTH

Дата: 2026-10-04. Ветка: `codex/assistant-rebirth`, HEAD: `07a022e`.
Проверки выполнены локально через `.venv` (Python 3.12.10) до изменения
исходного кода. На старте уже были изменены `README.md`, `docs/BACKLOG.md` и
неотслеживаемый `docs/ASSISTANT_REBIRTH_ROADMAP.md`; проверки их не меняли.

## Автоматические тесты

Из корня проекта:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

Результат: **411 тестов, OK**, exit code 0. Существующих падающих тестов нет.
В выводе есть ожидаемые `WARNING`/`ERROR` от тестов обработки ошибок (включая
недоступные embeddings, ошибку GitHub и ошибку SQLite); они не являются падением
тестового набора. Внешние API для этого набора не требуются.

## Автономные smoke-проверки

```powershell
.\.venv\Scripts\python.exe -m obs_chat_bot --sqlite-smoke
.\.venv\Scripts\python.exe -m obs_chat_bot --pipeline-smoke
.\.venv\Scripts\python.exe -m obs_chat_bot --analysis-smoke
```

Все три команды завершились с exit code 0. SQLite smoke проверяет начальную
схему, FTS5 и repository; pipeline и analysis используют fake-зависимости.
Каждая проверка создаёт временную SQLite-БД и не требует `.env` или внешних API.

Healthcheck также завершился с exit code 0 при тестовых значениях обязательных
настроек и временном `DATABASE_PATH`. Для воспроизведения без рабочей БД:

```powershell
.\.venv\Scripts\python.exe -c 'import os, sys, tempfile, subprocess; t = tempfile.TemporaryDirectory(prefix="obs-rebirth-health-"); e = os.environ.copy(); e.update({"APP_ENV": "development", "DATABASE_PATH": os.path.join(t.name, "health.db"), "TELEGRAM_BOT_TOKEN": "123456:ABCDEFGHIJKLMNOPQRSTUV", "OPENAI_BASE_URL": "https://example.com/v1", "OPENAI_API_KEY": "dummy-local-key", "OPENAI_MODEL": "dummy-local-model"}); code = subprocess.call([sys.executable, "-m", "obs_chat_bot", "--healthcheck"], env=e); t.cleanup(); raise SystemExit(code)'
```

Локального `.env` нет, поэтому это проверка механизма healthcheck, а не реальных
учётных данных. GitHub App и embeddings в ней отключены. Healthcheck не обращается
к API, не применяет миграции и не подтверждает полный пользовательский сценарий.
После всех проверок `data/app.db` отсутствовал, как и до них.

## Что не проверено локально

`scripts/check-all.ps1` запускает четыре smoke-команды через Docker Compose,
включая healthcheck с реальной конфигурацией. Его не запускали: локального
`.env` нет, а стандартный healthcheck открывает рабочий `data/app.db`. Те же
локально безопасные сценарии выше выполнены напрямую через Python.

Внешние интеграции требуют настроенных credentials и тестового vault: загрузка
реальной статьи (`--process-url`), живые Telegram/VK polling и ответы,
GitHub Device Flow и восстановление ожидающего подключения после перезапуска,
sync vault, реальный LLM/embedding API, подтверждение proposal и GitHub
write-back. `--process-url` дополнительно пишет в рабочую БД. Эти проверки
не выполнялись; их отсутствие не означает сбой приложения.
