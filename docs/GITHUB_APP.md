# Настройка GitHub App для Obsidian vault

GitHub connector читает выбранный repository и после явного подтверждения
пользователя записывает изменения Obsidian vault прямым commit в default branch.
Пользователь проходит Device Flow в браузере, а приложение сохраняет в SQLite
публичные ID/login одного GitHub-аккаунта и разрешённые installation IDs. Device code,
user access token, App JWT и
installation access token существуют только в памяти процесса.

## 1. Зарегистрировать GitHub App

Открыть настройки GitHub account:

1. `Settings` → `Developer settings` → `GitHub Apps` → `New GitHub App`.
2. Задать уникальное имя и Homepage URL проекта.
3. В `Webhook` отключить `Active`: webhook отложен в backlog.
4. В Repository permissions выбрать:
   - `Contents: Read and write`;
   - `Metadata: Read-only` — обязательное разрешение GitHub.
5. Остальные permissions оставить `No access`.
6. В `Where can this GitHub App be installed?` разрешить установку на любой
   account, если connector будет использоваться несколькими GitHub accounts.
7. Создать приложение.

Если `Contents` изменён с read-only после установки App, владелец installation
должен отдельно одобрить новые permissions. Пока запрос не одобрен, существующая
installation продолжает возвращать `Contents: read`.

На странице созданного App включить Device Flow. Client secret для выбранного
Device Flow не требуется.

## 2. Получить идентификаторы и PEM

На странице App сохранить:

- App ID;
- Client ID — он отличается от App ID;
- app slug из URL `https://github.com/apps/<app-slug>`.

В разделе private keys создать приватный ключ. Скачанный PEM поместить локально
в `data/github-app.pem`. Папка `data/` исключена из Git, но файл всё равно нельзя
публиковать, отправлять в чат или выводить в логи.

## 3. Настроить `.env`

```dotenv
GITHUB_APP_ID=123456
GITHUB_CLIENT_ID=Iv1.example
GITHUB_APP_SLUG=obs-chat-bot-example
GITHUB_PRIVATE_KEY_PATH=data/github-app.pem
```

GitHub connector считается выключенным, если все четыре значения отсутствуют.
Частично заполненная группа считается ошибкой конфигурации.

## Подготовить vault

До подключения создайте в корне выбранного vault `.knowledge-catcher.yml`
версии `1` с непустым списком `instructions`. Каждый путь должен вести к
существующему UTF-8 файлу относительно корня vault. Пример:

```yaml
version: 1
instructions:
  - rules.md
```

Создайте и `rules.md` с правилами ведения заметок. Расширенный
[пример в репозитории](../compadre_assistant/resources/.knowledge-catcher.yml)
ссылается на файлы `memory-bank/`: при его копировании эти файлы тоже должны
существовать. Бот не создаёт конфигурацию или правила автоматически.

## 4. Зарегистрировать пользователя и подключить vault

После запуска Telegram или VK adapter пользователь отправляет:

```text
/register
```

Затем бот просит имя профиля. Сохранение имени завершает регистрацию.
Подключение vault — отдельное действие внутри Obsidian. Войдите через
`/obsidian`, затем пришлите repository с Obsidian vault, либо сразу отправьте:

```text
/obsidian https://github.com/owner/repository
```

Если vault находится в подпапке, добавьте её после URL:
`https://github.com/owner/repository path/to/vault`. Используется default branch;
ссылка на `/tree/branch` не заменяет этот формат. Замена уже подключённого
vault требует ответа `да` или `нет`.

Дальнейшие шаги бот определяет сам:

1. если GitHub ещё не авторизован, бот выдаёт Device Flow URL и одноразовый код;
2. фоновый worker получает публичные ID/login GitHub-аккаунта и доступные installation
   IDs; временный пользовательский token в SQLite не сохраняется;
3. если App уже установлено и repository разрешён для записи, исходный vault
   подключается автоматически;
4. если App не установлено или repository не выбран, бот даёт installation URL;
   после настройки пользователь повторно присылает ссылку repository.

GitHub-аккаунт подключается к пользователю приложения, а не к отдельному каналу.
Если Telegram и VK привязаны к одному пользователю через `/link`, оба канала
автоматически используют тот же GitHub-аккаунт и будущий выбранный vault.

Одновременно для одного пользователя выполняется только один Device Flow. Если он
уже начат в другом привязанном канале, бот сообщает об этом вместо выдачи второго
одноразового кода.

Если процесс перезапущен до завершения Device Flow, временная сессия исчезает и
ссылку repository нужно отправить заново после истечения короткой защиты от
параллельного запуска (не более 20 минут от начала предыдущей попытки).

## Официальная документация

- [Регистрация GitHub App](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app)
- [Device Flow для GitHub App](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app)
- [JWT GitHub App](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app)
- [Installation access token](https://docs.github.com/en/rest/apps/apps#create-an-installation-access-token-for-an-app)
- [Одобрение новых permissions установленного GitHub App](https://docs.github.com/en/apps/using-github-apps/approving-updated-permissions-for-a-github-app)
