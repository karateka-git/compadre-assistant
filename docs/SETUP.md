# Подготовка проекта

## Получить код

Нужны Git и Docker Engine с Docker Compose (на Windows — Docker Desktop,
настроенный для Linux-контейнеров). Python на хосте нужен только для запуска
и тестов вне Docker; образ уже содержит Python 3.12.

```powershell
git clone https://github.com/karateka-git/obsChatBot.git
cd obsChatBot
Copy-Item .env.example .env
```

В Linux/macOS вместо последней команды используйте `cp .env.example .env`.
Все дальнейшие команды выполняются из корня клонированного репозитория.
Для чтения публичного репозитория доступ на push не требуется.

## Заполнить конфигурацию

`.env.example` — шаблон, который нужно заполнить: с `replace-me` и пустым
embedding key приложение не готово к запуску. Переменные читает
[`config.py`](../obs_chat_bot/core/data/config.py).

| Переменные | Назначение и обязательность |
| --- | --- |
| `APP_ENV`, `DATABASE_PATH` | Обязательны; в шаблоне `local` и `data/app.db`. Путь отсчитывается от рабочей папки процесса; в Docker она `/app`. |
| `ENABLED_CAPABILITIES` | Явный список ID через запятую, по умолчанию `obsidian`. Пустая строка отключает все входы. Изменение применяется после перезапуска. Текущие обязательные credentials и проверки конфигурации пока сохраняются; их разделение — шаг 2.5. |
| `APP_DEBUG` | Необязателен, по умолчанию `false`. |
| `TELEGRAM_BOT_TOKEN` | Токен от BotFather. Сейчас общий загрузчик требует его даже в режиме VK; healthcheck также проверяет его формат. |
| `TELEGRAM_PROXY_URL` | Необязательный URL SOCKS5/SOCKS4/HTTP-прокси для запросов Telegram Bot API, например `socks5://10.77.77.1:1080`. Пустое значение сохраняет прямой доступ; VK, GitHub и AI Gateway эту настройку не используют. Если прокси требует пароль, храните URL только в `.env`, вне Git. |
| `VK_BOT_TOKEN`, `VK_GROUP_ID` | Токен сообщества и положительный ID группы для VK. Для бота нужно настроить сообщения сообщества и Bots Long Poll. |
| `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL` | Обязательная конфигурация OpenAI-compatible LLM. Значения провайдера и моделей в шаблоне — настройки проекта, доступность нужно проверять у своего провайдера. |
| `EMBEDDING_BASE_URL`, `EMBEDDING_API_KEY`, `EMBEDDING_DOCUMENT_MODEL`, `EMBEDDING_QUERY_MODEL` | Для semantic search заполнить все четыре. Частично заполненная группа вызывает ошибку. |
| `EMBEDDING_PRICE_PER_MILLION_TOKENS`, `EMBEDDING_PRICE_CURRENCY`, `EMBEDDING_TARIFF_VERSION` | Необязательная группа оценки стоимости: заполнить все три или оставить все пустыми. Цена конечная и неотрицательная, валюта — три заглавные латинские буквы. |
| `CHUNK_MINIMUM_SIZE_CHARS`, `CHUNK_TARGET_SIZE_CHARS`, `CHUNK_MAXIMUM_SIZE_CHARS` | Необязательные положительные целые, по умолчанию `300`, `3000`, `6000`; требуется `minimum <= target <= maximum`. |
| `OBSIDIAN_CREATED_NOTE_TAG` | Необязательное имя служебного Obsidian-тега без `#`. Если переменная отсутствует, используется `knowledge-catcher`; значение `off` без учёта регистра отключает маркировку. Пустое или некорректное заданное значение вызывает ошибку конфигурации. |
| `GITHUB_APP_ID`, `GITHUB_CLIENT_ID`, `GITHUB_APP_SLUG`, `GITHUB_PRIVATE_KEY_PATH` | Заполнить все четыре для работы с vault или оставить всю группу пустой. Для полного сценария бота GitHub App необходима. |

**Особенность текущего Compose:** `docker-compose.yml` переопределяет
`OPENAI_API_KEY` значением `EMBEDDING_API_KEY`. Поэтому для штатного Docker-запуска
заполните `EMBEDDING_API_KEY` общим ключом AI Gateway; значение `OPENAI_API_KEY`
из `.env` внутри контейнера будет заменено. При прямом запуске Python ключи
независимы, `OPENAI_API_KEY` нужно заполнить отдельно.

Для отключения semantic search при прямом Python-запуске очистите все семь
`EMBEDDING_*` переменных, включая URL и модели из шаблона. Останется FTS-поиск.
При запуске через текущий Compose сначала потребуется изменить его
переопределение `OPENAI_API_KEY`, иначе пустой embedding key обнулит ключ LLM.

Создание GitHub App и получение PEM описаны в [GITHUB_APP.md](GITHUB_APP.md).
Поместите PEM в `data/github-app.pem`, а в `.env` задайте
`GITHUB_PRIVATE_KEY_PATH=data/github-app.pem`: каталог `data/` смонтирован в
контейнер как `/app/data`. Путь Windows вне этого тома контейнеру недоступен.
До подключения vault подготовьте обязательные правила по той же инструкции.

Служебный тег добавляется только новым заметкам из предложений `add` и только в
YAML frontmatter; при отсутствии frontmatter приложение создаёт его. `update`
не маркирует пользовательские заметки задним числом и сохраняет исходные
frontmatter tags уже существующей заметки. Изменение настройки не выполняет
массовую маркировку старых файлов. При `OBSIDIAN_CREATED_NOTE_TAG=off` приложение
не добавляет marker; для безопасного `update` оно также не принимает новые tags
от LLM, поскольку прежнее имя marker определить невозможно.

## Запустить нужный канал

После запуска Docker Engine:

```powershell
docker compose up --build tg_catcher
```

Для VK используйте `docker compose up --build vk_catcher`. Для обоих каналов —
`docker compose up --build`; оба набора channel credentials должны быть заполнены.
Compose не отключает сервис автоматически, если его токен отсутствует.
Каталог `data/` хранит рабочую SQLite и PEM вне образа.

Дальнейшие проверки, пользовательские команды и диагностика — в
[RUN.md](RUN.md) и [OPERATIONS.md](OPERATIONS.md).

## Разработка без Docker

Используйте Python 3.12, как в Dockerfile. В Windows:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe -m obs_chat_bot --sqlite-smoke
.\.venv\Scripts\python.exe -m obs_chat_bot --pipeline-smoke
.\.venv\Scripts\python.exe -m obs_chat_bot --analysis-smoke
```

В Linux/macOS создайте среду через `python3.12 -m venv .venv` и используйте
`.venv/bin/python`. Активация среды при таких командах не нужна.
Тесты используют `unittest`; отдельная установка pytest не требуется.
Smoke-режимы используют временные базы и fake-зависимости, не требуют `.env`
и не проверяют реальные API. Боты запускаются той же командой Python с флагом
`--telegram-bot` или `--vk-bot`, уже с заполненной `.env`.

Прямые runtime-зависимости закреплены в [requirements.txt](../requirements.txt),
но транзитивные зависимости отдельным lock-файлом не зафиксированы.
SQLite входит в Python; сборка SQLite должна поддерживать FTS5.
