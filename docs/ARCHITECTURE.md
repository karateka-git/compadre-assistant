# Архитектура Knowledge Catcher

## Назначение

Knowledge Catcher принимает ссылки на статьи из мессенджера, извлекает и
анализирует их текст, сопоставляет материал с Obsidian vault в GitHub и готовит
предложение изменения заметки. Изменение попадает в vault только после явного
подтверждения пользователя.

Каждый пользовательский профиль может быть доступен из нескольких внешних
каналов; данные профилей изолированы друг от друга. До рефакторинга на VPS
проверили полный VK-сценарий и ответ на команду в Telegram через отдельный
исходящий путь WireGuard и SOCKS5. Описанная ниже структура шага 2.1 проверена
локально и пока не развёрнута на VPS; прежние результаты записаны в
[истории](HISTORY.md#доступ-telegram-bot-api-с-российских-vps).

## Текущая структура после шага 2.1

```mermaid
flowchart LR
    TG[Telegram Bot API] --> TA[Telegram adapter]
    VK[VK Bots Long Poll] --> VA[VK adapter]
    CLI[CLI: process-url и smoke] --> OB[Obsidian bootstrap и smoke]
    OB --> OD[Obsidian domain: модели, сценарии и ports]
    TA --> IN[Core presentation: общий вход и диспетчер]
    VA --> IN
    IN --> MODE[(Режим пользователя в чате канала)]
    IN --> OP[Obsidian presentation: команды, URL и ответы]
    OP --> OD
    ODATA[Obsidian data: SQLite, HTTP, GitHub, LLM и embeddings] -. реализует ports .-> OD
    ODATA --> DB[(Единая SQLite-БД)]
    IN --> USERS[Core domain: пользователи и identity use cases]
    CDATA[Core data: config и SQLite repositories] -. реализует ports .-> USERS
    CDATA --> MODE
    CDATA --> DB
    ODATA --> GH[GitHub App и Obsidian vault]
    ODATA --> DC[Независимый document_chunker]
```

| Часть | Ответственность |
| --- | --- |
| `core/domain/` | Пользователи, identities, общие use cases и ports; ключ и ID активного режима чата. |
| `core/data/` | Конфигурация, SQLite connection/schema lifecycle, отдельные DTO, mappers и repositories профилей и режимов. |
| `core/presentation/` | Нейтральное событие, общие команды, диспетчер и форматирование общих ответов. |
| `capabilities/obsidian/domain/` | Модели, правила, use cases и ports статей, vault, поиска и proposals. |
| `capabilities/obsidian/data/` | Предметные SQLite DTO, mappers и repositories; HTTP, GitHub, LLM, embeddings, extraction, YAML-конфигурация vault и adapter `document_chunker`. |
| `capabilities/obsidian/presentation/` | Команды, обработка текста и подтверждений, форматирование ответов Obsidian и статический пример правил vault. |
| `capabilities/obsidian/bootstrap.py` | Сборка зависимостей возможности и её публичного входа. |
| `presentation/telegram/`, `presentation/vk/`, `presentation/cli/` | Адаптеры каналов и CLI вне presentation возможностей. |

Общий `IncomingEvent` несёт текст и адрес канала без `article_id` и полей
хранения. `IncomingMessageDispatcher` разрешает identity, обрабатывает общие
команды и приоритет подтверждения смены связи каналов, затем передаёт событие
публичному входу возможности. Конкретный вход и его устойчивый ID внедряет
composition root; `core` не импортирует Obsidian и не знает его ID.
`incoming_messages`, `processing_errors` и преобразование события в модель
статьи находятся внутри Obsidian. Каждое общее сообщение не журналируется.
Telegram и VK получают готовый `IncomingResponse` и доставляют фоновый ответ
в исходный чат.

Одна начальная SQL-схема находится в
`obs_chat_bot/core/data/sqlite/migrations/0001_initial_schema.sql`. Ядро владеет
таблицами пользователей, identities и `active_chat_modes`. Ключ режима —
`app_user_id`, `channel`, `chat_id`; значение — `capability_id` и технический
`updated_at`. Состояние vault, GitHub, индексов, proposals и журнала статьи
принадлежит Obsidian. Отключение возможности не очищает эти данные.
`document_chunker/` остаётся независимой библиотекой.

Это промежуточная сборка шага 2.1. Пока подключён единственный вход Obsidian:
первое предметное сообщение атомарно сохраняет маршрут по умолчанию, а
последующее сообщение читает его из SQLite. Другой процесс Telegram/VK
получает тот же выбор конкретного чата. Разные каналы, чаты и пользователи
имеют отдельные режимы. Сохранённый неизвестный ID не заменяется молча:
диспетчер возвращает нейтральное сообщение о недоступном маршруте; общая
справка остаётся доступна. Реестр, `enter`/`handle`, `/exit`, `/obsidian` и
новая семантика регистрации относятся к шагам 2.2–2.4. Конфигурация включения
возможностей относится к 2.5; Ask — к этапу 3.

Схема расширена таблицей режимов. Уже созданная development-БД требует
пересоздания перед отдельным запуском по правилам `AGENTS.md`: изменение
`0001` не обновляет существующую БД. Во время этой реализации рабочая БД
не открывалась и не очищалась; проверки использовали временные файлы.

### Composition root

`obs_chat_bot/bootstrap.py` собирает общие identity-зависимости, открывает
SQLite-соединение для отдельного сообщения и подключает публичный вход Obsidian.
Его предметные фабрики находятся в `capabilities/obsidian/bootstrap.py`.
`presentation/cli/main.py` запускает адаптеры и диагностические команды через
эту сборку; автономные smoke-сценарии Obsidian находятся в
`capabilities/obsidian/smoke.py`.

Docker Compose запускает два независимых worker-контейнера из одного образа:
`tg_catcher` с Telegram polling и `vk_catcher` с VK Long Poll. Оба используют
общий том `data/` с SQLite и ключом GitHub App. Каждый adapter преобразует
сообщение платформы в `IncomingEvent`. Для долгой операции он может отправить
раннюю квитанцию по предикату, полученному при сборке Obsidian. Результат и
фоновое завершение форматируются до нейтрального ответа каналу.

## Основные компоненты

### Пользователи и каналы

`UserIdentityService` создаёт внутреннего пользователя через `/register` и
хранит отдельные внешние identities для Telegram и VK. Команда `/link_code`
создаёт короткий код, а `/link <код>` связывает второй канал с тем же
пользователем. Статьи, vault, заметки и поисковые индексы ограничены
`app_user_id`, поэтому данные разных пользователей не смешиваются.

### GitHub App и подключение vault

GitHub Device Flow подтверждает, что пользователь авторизовал свой
GitHub-аккаунт. `GitHubConnectionCoordinator` ожидает результат в фоне и
сохраняет account и IDs доступных App installations в SQLite. Для проверки,
чтения и записи выбранного vault GitHub App выпускает installation token с
доступом к конкретному репозиторию. Такой token не хранится в БД и не выводится
в логи.

Пользователь отправляет URL репозитория после `/register`. Сервис проверяет,
что App установлено на этот репозиторий с правом `Contents: read and write`,
затем сохраняет выбранный vault и запускает первую синхронизацию. В корне
vault ожидается `.knowledge-catcher.yml` версии 1 с путями к instruction-файлам.

Device Flow session, включая challenge и callback исходного чата, хранится
только в памяти процесса. Межпроцессный SQLite claim предотвращает
одновременный запуск для одного пользователя. После перезапуска прежняя
session не восстанавливается; когда истечёт claim, пользователь может начать
новую попытку Device Flow. Это не продолжение прежней авторизации.

### Локальная копия vault и поиск

GitHub-репозиторий является источником истины для Obsidian vault. SQLite хранит
его локальную рабочую копию: Markdown-заметки, файлы правил, метаданные,
chunks, FTS5 и embeddings. При большом числе изменившихся файлов GitHub
snapshot скачивается одним ZIP-архивом конкретного commit при соблюдении
безопасных лимитов; иначе читаются только изменившиеся blobs. Новый snapshot
проверяется до применения, поэтому ошибка конфигурации или загрузки не
заменяет последнюю пригодную локальную копию.

Markdown разбирается пакетом `document_chunker` в устойчивые chunks с
заголовочным контекстом и хешем содержимого. SQLite FTS5 даёт лексический
поиск по chunks. Semantic-индекс хранит вектор как `float32` BLOB, связанный
с chunk, его хешем и моделью. Неизменившиеся chunks переиспользуют сохранённые
embeddings. Гибридный поиск сочетает BM25 и cosine similarity через
Reciprocal Rank Fusion. Если semantic-индекс не готов или embeddings временно
недоступны, используется явный FTS fallback; его статус показывается в
ответах синхронизации.

### Pipeline статьи и proposal

1. HTTP-adapter безопасно загружает страницу, не допуская private, loopback и
   link-local адреса даже после redirect.
2. `trafilatura` извлекает содержательный текст. Статья и связанное с ней
   входящее сообщение сохраняются в SQLite.
3. OpenAI-compatible LLM создаёт и сохраняет анализ статьи.
4. Obsidian проверяет актуальность vault до обработки URL (не чаще раза в
   шесть часов). После анализа hybrid search подбирает релевантные заметки,
   а LLM с правилами vault готовит `add`, `update` или `skip`.
5. Пользователь видит preview Markdown и отвечает `да` либо `нет`. До `да`
   GitHub не меняется.

```mermaid
sequenceDiagram
    actor U as Пользователь
    participant C as Telegram или VK adapter
    participant I as Общий вход
    participant UDB as Общий user repository
    participant O as Obsidian presentation
    participant D as Obsidian data
    participant G as GitHub App и vault

    U->>C: /register и имя
    C->>I: IncomingEvent
    I->>UDB: создать профиль и identity
    U->>C: URL vault
    C->>I: IncomingEvent
    I->>O: идентифицированный пользователь и событие
    O->>G: Device Flow и выбор vault
    G-->>O: authorization и доступная installation
    O->>D: сохранить vault и локальный индекс
    O-->>C: нейтральный ответ в исходный чат

    U->>C: URL статьи
    C->>I: IncomingEvent
    I->>O: идентифицированный пользователь и событие
    O->>D: сохранить сообщение, статью, анализ и proposal
    O-->>C: preview и запрос «да/нет»
    C-->>U: предложение изменения
    U->>C: да
    C->>I: IncomingEvent
    I->>O: идентифицированный пользователь и событие
    O->>G: preflight SHA и commit
    O->>D: отметить proposal и синхронизировать индекс
    O-->>C: результат write-back
```

### Подтверждённый write-back

`ObsidianProposalConfirmationService` применяет только pending proposal.
Перед записью он читает актуальное состояние target-файла в GitHub и проверяет
SHA. Если другой автор уже изменил файл, proposal переходит в `conflict`, а
чужой Markdown сохраняется. При неоднозначном ответе сети сервис сопоставляет
remote Markdown с ожидаемым: если commit уже создан, локальная транзакция
завершается без повторной записи. После удачного commit запускается
синхронизация vault, чтобы SQLite, FTS и embeddings соответствовали GitHub.

## Гарантии и ограничения

- **Согласованность локального индекса.** Source SHA snapshot продвигается
  после успешной проверки и сохранения. Marker готовности semantic-индекса
  публикуется после полного набора vectors; частичные checkpoints будут
  продолжены при следующей синхронизации и не используются semantic search.
- **Один владелец операции.** SQLite lease координирует Telegram и VK: для
  одного vault одновременно выполняется одна синхронизация или write-back.
  Истёкший lease можно захватить повторно.
- **Явное изменение GitHub.** LLM не пишет в vault напрямую. Сначала
  сохраняется proposal, затем пользователь подтверждает его. Ошибка write-back
  оставляет proposal pending для повтора; конфликт не затирает изменения.
- **Сохранность данных.** Production SQLite и PEM находятся на постоянном
  диске VPS вне Docker-образа. На сервере выполняется ежедневная SQLite
  backup-копия с проверкой целостности и хранением семи последних экземпляров.
  Резервирование VM вне сервера остаётся отдельной задачей эксплуатации.
- **Граница проверок.** После шага 2.1 прошли 436 автономных unittest,
  SQLite/pipeline/analysis smoke и healthcheck на временной БД. Живые каналы,
  GitHub Device Flow, sync/write-back и внешние LLM/embeddings API с новой
  структурой без credentials и тестового vault не проверялись. Прежние
  production-проверки описаны в [истории](HISTORY.md).

## Связанные документы

- [ASSISTANT REBIRTH ROADMAP](ASSISTANT_REBIRTH_ROADMAP.md) — этапы перехода.
- [RUN.md](RUN.md) — локальный запуск, smoke-проверки и команды.
- [OPERATIONS.md](OPERATIONS.md) — эксплуатация VPS, обновление, логи и backup.
- [GITHUB_APP.md](GITHUB_APP.md) — создание и настройка GitHub App.
- [DOCUMENT_CHUNKER.md](DOCUMENT_CHUNKER.md) — правила разбиения Markdown.
- [DECISIONS.md](DECISIONS.md) — прежние архитектурные решения.
- [BACKLOG.md](BACKLOG.md) — открытые задачи.
- [HISTORY.md](HISTORY.md) — завершённые задачи и прежние критерии.
