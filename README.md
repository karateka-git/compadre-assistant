# Compadre Assistant

Compadre Assistant — личный ассистент в Telegram и VK с возможностями Obsidian и Ask. В режиме Obsidian он помогает превращать статьи в пополняемый vault. Он принимает ссылку, извлекает и анализирует материал, ищет связанные заметки, предлагает изменение в Markdown и записывает его в GitHub только после явного подтверждения пользователя.

## Статус

Статус на 2026-10-13: проект выпущен, переход Assistant Rebirth, включая
этап 4, закрыт по решению пользователя. Production на VPS переключён на
Compadre Assistant: Telegram-бот `@CompadreAssistant_bot` и сообщество VK
`vk.com/compadre_assistant`. Оба контейнера `tg_assistant` и `vk_assistant`
прошли проверку healthy после перезапуска; Telegram polling и VK Long Poll
подтверждены. Telegram работает через отдельный WireGuard-туннель и SOCKS5-прокси.

Реализованы режимы по чатам, адресные операции, независимые настройки
каналов/возможностей и Ask. Пользовательский Obsidian flow в Telegram прошёл
анализ статьи, review и подтверждённую запись в vault `karateka-git/my_obs_data`.
Подробности перехода и проверок — в [production-инструкции](docs/PRODUCTION.md)
и [итоге Assistant Rebirth](docs/ASSISTANT_REBIRTH_ROADMAP.md#47-сверить-итог-перехода).

Репозиторий переименован в
[`karateka-git/compadre-assistant`](https://github.com/karateka-git/compadre-assistant).
Существующая GitHub App переименована в **Compadre Assistant**, её действующий
slug — `compadre-assistant`; прежний App ID и доступ к vault сохранены.

Проверка 2026-10-10: 548 unittest, три автономных
smoke и healthcheck локального профиля с временной БД прошли. Read-only API
подтвердили внешние имена и чтение существующего vault через прежний PEM после
переименования App. Локально на новом Telegram-боте с временной БД проверены
`/start`, `/register` с сохранением имени и `/ask`. Контейнерная сборка,
CLI smoke и healthcheck прошли без сети на временной БД и фиктивном профиле;
контейнерный polling в рамках той проверки не запускался.

Дальнейшая работа перечислена в [бэклоге](docs/BACKLOG.md): среди возможных задач — фоновое ревью статей и переход на внешнюю PostgreSQL для App Platform. Завершённые работы сохранены в [истории](docs/HISTORY.md).

## Как это работает для пользователя

1. Отправьте `/register` в доступном канале и задайте имя профиля.
2. Войдите через `/obsidian`, подготовьте [обязательные правила vault](docs/GITHUB_APP.md#подготовить-vault), пришлите ссылку на GitHub-репозиторий и пройдите предложенную ботом авторизацию GitHub. Бот проверит права GitHub App и синхронизирует заметки.
3. Отправьте ссылку на статью. Бот извлечёт текст, подготовит анализ и найдёт связанные заметки vault.
4. Бот покажет proposal: добавить заметку, обновить существующую или пропустить запись.
5. Подтвердите или отмените предложение командой из ответа в исходном чате и режиме Obsidian. При единственном ожидании этого чата можно ответить `да` или `нет`; при нескольких используйте команду с ID и версией. При подтверждении бот запишет предложенное добавление или обновление commit в GitHub; предложение `skip` завершится без записи. При отмене vault останется без изменений.

После выхода или в другом чате отправьте `/obsidian`, чтобы выбрать работу с vault.
Повторная отправка той же статьи внутри режима использует сохранённый результат, если он уже есть. Для ручной синхронизации vault доступна команда `/github_sync`.
Разные статьи связанных TG/VK-чатов проходят анализ независимо и имеют отдельные предложения. `/exit` сохраняет ожидающие действия; повторный `/obsidian` показывает только ожидания исходного чата. Перенос identity подтверждается отдельной командой `/confirm core identity …`, которую возвращает `/link`.

По умолчанию новые заметки из предложений `add` получают служебный Obsidian-тег
`knowledge-catcher` в YAML frontmatter. Inline-marker в тексте не добавляется.
При `update` пользовательская заметка не получает этот тег задним числом, а
уже помеченная ботом заметка сохраняет marker. Существующие старые заметки
автоматически не маркируются.

Команда `/ask <вопрос>` даёт разовый ответ без истории переписки и не меняет
режим чата. Доступные возможности и команды перечислены в `/help`.

## Техническая схема

```mermaid
flowchart LR
    Channels[VK / Telegram] --> Adapters[Адаптеры каналов]
    Adapters --> Incoming[Core presentation: identity и диспетчер]
    Incoming --> Modes[(Режим конкретного чата)]
    Incoming --> Obsidian[Obsidian presentation: команды и ответы]
    Obsidian --> Domain[Obsidian domain: модели, сценарии и ports]
    Incoming --> Ask[Ask presentation: команда и ответ]
    Ask --> AskDomain[Ask domain: вопрос и port модели]
    CLI[CLI: диагностика и process-url] --> Bootstrap[Общий bootstrap.py]
    Bootstrap --> ObsidianBootstrap[Obsidian bootstrap и smoke]
    ObsidianBootstrap --> Domain
    Bootstrap --> AskBootstrap[Ask bootstrap]
    AskBootstrap --> AskDomain
    ObsidianData[Obsidian data: SQLite, GitHub, HTTP, LLM, embeddings] -. реализует ports .-> Domain
    AskData[Ask data: конфигурация и LLM-клиент] -. реализует port .-> AskDomain
    Incoming --> Users[Core domain: пользователи и identities]
    CoreData[Core data: config и SQLite] -. реализует ports .-> Users
    CoreData --> Modes
    ObsidianData --> SQLite[(Единая SQLite-БД)]
    ObsidianData --> GitHub[GitHub App и Obsidian vault]
    ObsidianData --> Chunker[Независимый document_chunker]
    AskData --> AskProvider[Провайдер LLM]
```

Модули разделены на `domain`, `data` и
`presentation`; возможности явно регистрируются по конфигурации. Регистрация
завершается созданием профиля и именем. `/obsidian [текст или URL]` включает
режим текущего чата и обрабатывает аргумент, `/exit` завершает его без удаления
данных. Подключение GitHub/vault выполняется внутри Obsidian. Вне режима текст
и URL предлагают доступные входы. Режим сохраняется между процессами по
пользователю, каналу и ID чата. `ENABLED_CAPABILITIES=obsidian` включает Obsidian;
пустое значение отключает возможности, сохраняя общие команды профиля.
`ENABLED_CHANNELS=telegram,vk` выбирает каналы для scripts и общего healthcheck;
отдельный запуск бота проверяет только его канал. При отключённом Obsidian
его настройки полностью игнорируются и клиенты не создаются. Пустые группы
GitHub/LLM позволяют пользоваться профилем; Obsidian сообщает о недоступном
провайдере. Частичные или некорректные включённые группы блокируют запуск.
Без embeddings используется FTS fallback; ключи LLM и embeddings независимы.
Ask доступен через `/ask <вопрос>` после регистрации и имени, без GitHub/vault.
Для включения задайте `ENABLED_CAPABILITIES=ask` либо `obsidian,ask` и отдельные
`ASK_BASE_URL`, `ASK_API_KEY`, `ASK_MODEL` (см. [SETUP](docs/SETUP.md#настройка-ask)).
Каждый вопрос независим; бот подтверждает приём и присылает ответ частями.
Режим Obsidian и ожидающие подтверждения сохраняются. Подробная схема находится в
[ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Быстрый старт

Для локальной подготовки смотрите [SETUP.md](docs/SETUP.md), для запуска и проверок — [RUN.md](docs/RUN.md). Подключение GitHub App и vault описано в [GITHUB_APP.md](docs/GITHUB_APP.md). Секреты хранятся только в локальном `.env` и каталоге `data/`, в Git они не попадают.

## Production VPS

Порядок эксплуатации production-сервиса, обновления, диагностика и резервные копии описаны в [OPERATIONS.md](docs/OPERATIONS.md).

## Документация

- [ASSISTANT REBIRTH ROADMAP](docs/ASSISTANT_REBIRTH_ROADMAP.md) — завершённый переход к модульному личному ассистенту и результаты проверок.
- [Архитектура](docs/ARCHITECTURE.md) — слои, компоненты и потоки данных.
- [Подготовка](docs/SETUP.md) — установка, `.env`, выбор канала и среда разработки.
- [Запуск и проверки](docs/RUN.md) — локальный запуск, healthcheck и smoke-сценарии.
- [Эксплуатация](docs/OPERATIONS.md) — production VPS, обновления, логи и backup.
- [Production VPS](docs/PRODUCTION.md) — контекст доступа и текущее состояние сервера без секретов.
- [GitHub App](docs/GITHUB_APP.md) — настройка прав и подключение Obsidian vault.
- [Бэклог](docs/BACKLOG.md) — задачи, которые ещё не начаты.
- [История](docs/HISTORY.md) — завершённые задачи и прежние критерии разработки.
- [Журнал решений](docs/DECISIONS.md) — архитектурные решения и их причины.
- [Document chunker](docs/DOCUMENT_CHUNKER.md) — выделенный модуль разбиения документов.
