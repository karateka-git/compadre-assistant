# Представление сообщений в Telegram и VK — проект решения

Этот документ показывает предлагаемый контракт ответа на конкретных моделях
данных: как [тексты из реестра](RESPONSE_MAP.md) превращаются в сообщение и как
адаптеры выводят его по [правилам сообщений](MESSAGE_RULES.md). Код ниже —
эскиз будущего API, а не описание уже реализованных классов. Возможности
оформления VK для сообщений бота нужно подтвердить отдельно.

## Что передаётся адаптеру сейчас

`IncomingResponse` содержит `text: str`, `error` и набор
`ConfirmationAction`. Метод `render_text()` добавляет адресные команды
действий к строке. Telegram и VK отправляют результат как обычный текст —
как при немедленном ответе, так и при доставке `BackgroundReply`.

```text
IncomingResponse(text="Путь: notes")
    → render_text()
    → "Путь: notes"
    → адаптер Telegram или VK
```

Строка не сообщает адаптеру, что `notes` — значение для копирования. Если
вписать в неё `<code>notes</code>`, VK получит и эти символы. Поэтому текст
и пометки оформления предлагается передавать отдельными полями.

## Какие поля будут у сообщения

Минимальная модель в `core/presentation`:

```python
class TextStyle(StrEnum):
    PLAIN = "plain"        # Обычный текст.
    EMPHASIS = "emphasis"  # Короткий смысловой акцент.
    CODE = "code"          # Команда, путь или значение для копирования.


@dataclass(frozen=True, slots=True)
class TextSpan:
    text: str
    style: TextStyle = TextStyle.PLAIN


@dataclass(frozen=True, slots=True)
class Paragraph:
    spans: tuple[TextSpan, ...]


@dataclass(frozen=True, slots=True)
class ListItem:
    spans: tuple[TextSpan, ...]


@dataclass(frozen=True, slots=True)
class BulletList:
    items: tuple[ListItem, ...]


@dataclass(frozen=True, slots=True)
class MessageContent:
    blocks: tuple[Paragraph | BulletList, ...]
```

`MessageContent.blocks` задаёт порядок абзацев и списков. У каждого абзаца
или пункта списка есть `spans` — последовательность текстовых фрагментов.
`TextSpan.text` содержит символы, `TextSpan.style` — пожелание к их виду.
Это типизированные данные: собственного языка тегов и парсера строк нет.

Для короткого ответа достаточно одного `Paragraph` с одним `TextSpan`.
Короткий акцентный абзац может служить заголовком. Отдельные типы для
заголовков, ошибок и каждого сценария пока не нужны; новый тип добавляем,
когда реальный ответ нельзя ясно собрать из этих полей.

`IncomingResponse` получает `content: MessageContent` вместо `text: str`:

```python
@dataclass(frozen=True, slots=True)
class IncomingResponse:
    content: MessageContent
    error: Exception | None = None
    actions: tuple[ConfirmationAction, ...] = ()
    suggested_actions: tuple[ReplyAction, ...] = ()
```

`ConfirmationAction(action_id, label, command)` остаётся отдельной моделью.
Её `command` хранит полный текстовый способ принять или отклонить конкретное
действие. Адаптер может показать кнопку, а при отсутствии кнопок — вывести
`label` и `command` отдельными строками после `MessageContent`. Он не
разбирает смысл подтверждения. Поле
`suggested_actions` описано в [проекте подсказок](COMMAND_HINTS.md): оно
позволяет показывать под конкретным ответом только уместные следующие
действия. Для него действует тот же текстовый путь. Оно не заменяет полный
контекстный `/help`. После перевода
всех вызывающих мест прежний `render_text()` удаляется.

## Как presentation собирает ответ

Например, Obsidian сообщает об успешном подключении хранилища:

```python
content = MessageContent(blocks=(
    Paragraph(spans=(
        TextSpan("Хранилище заметок (vault) подключено.", TextStyle.EMPHASIS),
    )),
    Paragraph(spans=(
        TextSpan("Репозиторий: "),
        TextSpan("owner/repo", TextStyle.CODE),
    )),
    Paragraph(spans=(
        TextSpan("Путь: "),
        TextSpan("notes", TextStyle.CODE),
    )),
))
response = IncomingResponse(content=content)
```

Это пример формы данных, а не утверждённая новая текстовка. Слова и условия
показа определяет `capabilities/obsidian/presentation`. Для `/help` свой
`MessageContent` собирает `core/presentation`, для Ask —
`capabilities/ask/presentation`. Они не вставляют HTML Telegram или разметку
VK в `TextSpan.text`.

Для точного оформления команд справки `HelpSection` передаёт записи команды
отдельными полями, а не готовыми строками. Канонический эскиз записи
`CapabilityCommand` и его параметров приведён в
[проекте подсказок](COMMAND_HINTS.md#публичный-каталог-команд-возможностей).
Тогда ядро создаёт `TextSpan("/ask", CODE)` без поиска команды в тексте и
строит синтаксис из точного имени и параметров. Выбор доступных разделов
`/help` остаётся правилом ядра.

Имя, URL, ID, путь, diff, статья и результат LLM подставляются в `text` как
данные. Текст модели и Markdown статьи не становятся инструкциями
оформления. Для их специального оформления позднее понадобится отдельное
явное преобразование.

## Как адаптер читает модель

Граница между `presentation` и адаптером — `IncomingResponse` с
`MessageContent` и действиями. Это общий интерфейс данных. Отдельный `Protocol` для
рендереров пока не нужен: каждый адаптер вызывает свою функцию вывода.

```mermaid
flowchart LR
    P[Presentation создаёт IncomingResponse] --> C[MessageContent: blocks и spans]
    C --> T[Telegram adapter: обход blocks и spans]
    C --> V[VK adapter: обход blocks и spans]
    T --> TS[TelegramChunk: text и parse_mode]
    V --> VS[VkChunk: text и параметры VK]
```

Для `Paragraph` адаптер последовательно обрабатывает `spans`. Для
`BulletList` — каждый `ListItem` и его `spans`, добавляя маркер пункта.
Между блоками ставит переносы строк. Каналы различаются обработкой `style`:

```python
# Эскиз внутри Telegram-адаптера, не готовая реализация.
def render_span(span: TextSpan) -> str:
    safe_text = html.escape(span.text)
    if span.style == TextStyle.EMPHASIS:
        return f"<b>{safe_text}</b>"
    if span.style == TextStyle.CODE:
        return f"<code>{safe_text}</code>"
    return safe_text
```

Telegram-адаптер передаёт собранный текст с `parse_mode="HTML"` и экранирует
**каждый** `TextSpan.text`, включая данные из внешних источников. VK-адаптер
проходит по тем же полям. Он применяет только подтверждённое для отправки
ботом оформление; при отсутствии подходящего варианта выводит `span.text`
обычным текстом. Отдельная обязательная функция `render_plain` не нужна.

В примере выше Telegram получит:

```html
<b>Хранилище заметок (vault) подключено.</b>

Репозиторий: <code>owner/repo</code>

Путь: <code>notes</code>
```

Если нужное оформление VK недоступно, его адаптер отправит:

```text
Хранилище заметок (vault) подключено.

Репозиторий: owner/repo

Путь: notes
```

Оформление выбирается **до отправки**. После неопределённой ошибки API не
следует повторно отправлять тот же ответ обычным текстом: первый вариант
мог уже дойти до пользователя.

## Где находятся модели и как доставляются ответы

`MessageContent`, его блоки и сборка `ConfirmationAction` принадлежат
`core/presentation`. Тексты Ask и Obsidian собирает `presentation`
соответствующей возможности. Вывод в канал находится внутри существующих
`presentation/telegram` и `presentation/vk`. Адаптеры не выбирают условия
показа текста и не знают предметных сценариев. Новые модели не требуются в
`domain`, `data`, SQLite или bootstrap.

Один и тот же вывод внутри каждого адаптера обслуживает немедленный
`IncomingResponse` и `BackgroundReply.response`, приходящий через
`ReplyDeliveryPort`. Короткие ответы адаптера до входа в ядро, например
просьбу прислать текст, он может собрать из `MessageContent` локально.

Каждый адаптер возвращает готовые к отправке части своего канала:

```text
Telegram adapter: render(response, limit) → tuple[TelegramChunk, ...]
VK adapter:       render(response, limit) → tuple[VkChunk, ...]
```

`TelegramChunk` хранит текст и нужный `parse_mode`, `VkChunk` — текст и
подтверждённые параметры отправки VK. Адаптер другого канала может
возвращать только текстовые части: модель ответа не требует от него меню,
кнопок или богатого оформления. Для длинного ответа адаптер делит
блоки **до окончательной сериализации**; слишком большой блок делит по
безопасным текстовым границам. Команду подтверждения, URL и путь нельзя
обрывать посередине. После разбиения каждый Telegram-фрагмент получает
собственную корректную HTML-разметку. Длину проверяют по готовому
фрагменту; порядок отправки и существующие повторы доставки сохраняются.

## Что проверить для VK

Сейчас VK-адаптер передаёт в `messages.send` строку `message`. В
[схеме VK API 5.199](https://github.com/VKCOM/vk-api-schema/blob/master/messages/methods.json)
у `messages.send` не указан параметр `parse_mode` или `entities`. Это не
доказывает отсутствие других способов оформления. Перед реализацией
VK-рендерера нужно проверить актуальный контракт и реальное отображение
сообщения бота в тестовом диалоге: жирный текст, моноширинный текст,
ссылки, переносы строк и списки. По результату для каждого `TextStyle`
определить вывод внутри VK-адаптера.

## Порядок внедрения

1. Добавить `MessageContent` и его блоки; реализовать сборку действий.
   Проверить на примере, что оба адаптера получают одинаковые данные.
2. Реализовать обход блоков внутри Telegram и VK. Проверить экранирование
   переменных значений, переносы строк и читаемый вывод без оформления.
3. Перевести общие ответы, контекстный `/help`, Ask, Obsidian, немедленные и
   фоновые пути на `IncomingResponse.content`. Заполнить целевые тексты в
   реестре; уже удачные ответы отметить «без изменений». После перевода всех
   вызывающих мест удалить `render_text()` и строковые обходные пути.
4. Проверить сценарии справки, подтверждения, длинные ответы и смысловое
   совпадение Telegram/VK. В тестовом диалоге проверить вид оформления.

Представление ответа не меняет доступ пользователя, активный режим, смысл
команды, сохранность данных или результат предметного действия.
