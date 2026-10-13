# Переход production на Compadre Assistant

Это подготовленный runbook отдельного развёртывания, а не отчёт о выполнении.
Команды изменения VPS, публикации кода и переключения ботов при подготовке
документа не запускались. Desktop/browser UI не используется.
Перед исполнением перечитать [PRODUCTION.md](PRODUCTION.md) и `AGENTS.md`.
Сначала согласовать окно переключения и конкретный опубликованный commit.
`git push` требует отдельного явного согласия пользователя.

## Исходное состояние и готовые данные

Read-only проверка 2026-10-10 подтвердила:

- VPS `user@91.188.213.142`, checkout `/opt/obs-chat-bot`, ветка `main`,
  commit `77686d5221c39543885f35fe90b70b7e6bfae723`, рабочая копия чистая.
- `obs-chat-bot.service` и `obs-chat-bot-backup.timer` активны.
  Override `/etc/systemd/system/obs-chat-bot.service.d/channels.conf`
  выполняет `docker compose up -d vk_catcher tg_catcher` и
  `docker compose stop vk_catcher tg_catcher` из `/opt/obs-chat-bot`.
- Backup service имеет `Requires=obs-chat-bot.service`; его запуск способен
  поднять приложение. Для копии во время остановки вызываем непосредственно
  `/usr/local/lib/obs-chat-bot/backup_sqlite.py`. Скрипт применяет SQLite backup API,
  проверяет integrity, хранит семь копий `/var/backups/obs-chat-bot/app-*.db`.
  Timer запускается в 03:15 UTC, `Persistent=true`.
- Новый Compose содержит `tg_assistant`, `vk_assistant`, общий image
  `compadre-assistant:dev`, Python module `compadre_assistant`.
- Новый Telegram `@CompadreAssistant_bot`, ID `8870763711`; токен уже сохранён
  локально в игнорируемом `data/compadre-assistant-telegram.env`.
  Старый токен находится в production `.env` и понадобится только для rollback
  или отдельно согласованного уведомления.
- VK token сохраняется, group ID `240322403`, адрес `compadre_assistant`.
- GitHub App ID `4363317`, прежние Client ID/PEM/installation сохраняются;
  нужный slug `compadre-assistant`. Repo origin должен стать
  `git@github.com:karateka-git/compadre-assistant.git`.
- `/opt/obs-chat-bot/data/github-app.pem` и `/opt/obs-chat-bot/.env` имеют права
  `600`. Telegram proxy из серверного `.env` сохраняется: прямой доступ
  H3LLO к Telegram ранее не работал.

Пути checkout, systemd unit, SSH deploy key и backup сохраняем: они не зависят
от публичного имени. Меняем только Compose-команды в override.

## Что решить перед остановкой

1. Выбрать опубликованную ветку/commit новой версии. Локальная работа идёт в
   `codex/assistant-rebirth`; production сейчас на `main`. Не делать неявный
   merge/push. Предпочтительно утвердить конкретный SHA после публикации и
   развернуть detached HEAD этого SHA; последующие обновления также по SHA.
2. Назначить окно, подтвердить исполнение этого production-перехода с чистой
   SQLite. Пользователь уже определил очистку перед production; это отдельный
   шаг данного перехода, а не применение development-правила к серверу.
3. Заполнить постоянные `ASK_BASE_URL`, `ASK_API_KEY`, `ASK_MODEL` и включить
   `ENABLED_CAPABILITIES=obsidian,ask`. Пока постоянного Ask-профиля нет.
   Успешный локальный smoke использовал endpoint/model Obsidian и ключ embeddings
   того же endpoint, явно переданные в process env. Для production утвердить
   этот профиль либо предоставить другой через секретный файл; не выбирать
   ключ автоматически и не печатать его. Проверить квоту и доступ к модели.
4. Назначить тестовый vault и пользователя для Device Flow/подтверждения записи.
   Вход в GitHub и одобрение Device Flow пользователь выполняет сам.
5. Уведомление прежним ботом требует отдельного согласования текста, получателей
   и времени. Эта процедура не выполняет рассылку. Прежний бот не удаляется.

## 1. Подготовить секреты и артефакты

На локальном компьютере подготовить игнорируемый файл
`data/compadre-production-secrets.env` с **только** четырьмя ключами:
`TELEGRAM_BOT_TOKEN` из уже существующего файла нового бота и утверждённые
`ASK_BASE_URL`, `ASK_API_KEY`, `ASK_MODEL`. Использовать dotenv-совместимое
экранирование. Значения не вставлять в команды, чат, Git или логи.
Не включать туда прежний `.env` целиком. Проверить `git check-ignore` обоих
секретных файлов после `git status --short`.

Передать файл по SSH в закрытый staging-каталог (команды PowerShell):

```powershell
ssh -i "C:\Users\compadre\Downloads\SSH\H3LLO_CLOUD\karateka" user@91.188.213.142 'umask 077; mkdir -p /home/user/.compadre-deploy; chmod 700 /home/user/.compadre-deploy'
scp -i "C:\Users\compadre\Downloads\SSH\H3LLO_CLOUD\karateka" data/compadre-production-secrets.env user@91.188.213.142:/home/user/.compadre-deploy/secrets.env
ssh -i "C:\Users\compadre\Downloads\SSH\H3LLO_CLOUD\karateka" user@91.188.213.142 'chmod 600 /home/user/.compadre-deploy/secrets.env'
```

Далее все bash-блоки выполняются в **одной SSH-сессии**. Не включать `set -x`.
Подставить утверждённый SHA вместо маркера; не продолжать с маркером.

```bash
set -euo pipefail
umask 077
cd /opt/obs-chat-bot
git status --short
test -z "$(git status --porcelain)"
deploy_sha='REPLACE_WITH_APPROVED_PUBLISHED_SHA'
test "$deploy_sha" != REPLACE_WITH_APPROVED_PUBLISHED_SHA
old_sha=$(git rev-parse HEAD)
old_branch=$(git symbolic-ref --short -q HEAD || true)
old_origin=$(git remote get-url origin)
rollback_dir=$(mktemp -d /home/user/.compadre-deploy/rollback-XXXXXXXX)
export rollback_dir
printf '%s\n' "$old_sha" > "$rollback_dir/old-sha"
printf '%s\n' "$old_branch" > "$rollback_dir/old-branch"
printf '%s\n' "$old_origin" > "$rollback_dir/old-origin"
cp -p .env "$rollback_dir/old.env"
sudo cp /etc/systemd/system/obs-chat-bot.service.d/channels.conf "$rollback_dir/channels.conf"
sudo chown user:user "$rollback_dir/channels.conf"
systemctl cat obs-chat-bot.service obs-chat-bot-backup.service obs-chat-bot-backup.timer > "$rollback_dir/systemd-before.txt"
docker compose ps
old_image=$(docker inspect --format '{{.Image}}' "$(docker compose ps -q tg_catcher)")
test "$old_image" = "$(docker inspect --format '{{.Image}}' "$(docker compose ps -q vk_catcher)")"
docker tag "$old_image" compadre-assistant:rollback
docker compose config --images | sort -u > "$rollback_dir/old-image-tags"
git remote set-url origin git@github.com:karateka-git/compadre-assistant.git
git fetch origin
git cat-file -e "$deploy_sha^{commit}"
git branch -r --contains "$deploy_sha"
```

Проверить, что SHA принадлежит утверждённой опубликованной ветке. До смены кода
зафиксировать существующие имена образов: `docker compose images`; не выполнять
`docker compose config` без ограничения полей — он может раскрывать secrets.

## 2. Остановить старые каналы и сделать проверенный backup

```bash
sudo systemctl stop obs-chat-bot-backup.timer
sudo systemctl stop obs-chat-bot-backup.service
sudo systemctl stop obs-chat-bot.service
docker compose stop vk_catcher tg_catcher
test -z "$(docker compose ps --status running -q vk_catcher tg_catcher)"
systemctl is-active obs-chat-bot.service || true
/usr/local/lib/obs-chat-bot/backup_sqlite.py
```

Проверить успешный exit code backup и отдельно сохранить свежую согласованную
копию вне retention-каталога. После остановки всех writers SQLite backup API
также учитывает оставшийся WAL:

```bash
python3 - <<'PY'
import os, sqlite3
from pathlib import Path
target = Path(os.environ['rollback_dir']) / 'app-before.db'
assert not target.exists()
source = sqlite3.connect('file:/opt/obs-chat-bot/data/app.db?mode=ro', uri=True)
dest = sqlite3.connect(target)
source.backup(dest)
assert dest.execute('PRAGMA integrity_check').fetchone() == ('ok',)
dest.close(); source.close()
target.chmod(0o600)
print('Rollback SQLite integrity: ok')
PY
```

Не продолжать при ошибке, работающем writer или отсутствии backup.
Проверить отсутствие другого локального/серверного polling нового Telegram
токена. Проверочный локальный PID 2956 уже остановлен, но состояние перепроверить.

## 3. Переключить код, конфигурацию и systemd

```bash
git status --short
test -z "$(git status --porcelain)"
git switch --detach "$deploy_sha"
docker compose config --services
docker compose build tg_assistant
```

Ожидаемые сервисы: только `tg_assistant` и `vk_assistant`. Обновить `.env`
атомарно через Python внутри нового image; секреты читаются из файлов:

```bash
docker run --rm -i --network none \
  --mount type=bind,src=/opt/obs-chat-bot,dst=/target \
  --mount type=bind,src=/home/user/.compadre-deploy/secrets.env,dst=/run/deploy.env,readonly \
  compadre-assistant:dev python - <<'PY'
from pathlib import Path
from dotenv import dotenv_values, set_key
import os, shutil
source = Path('/target/.env'); temporary = Path('/target/.env.deploy-tmp')
secrets = dotenv_values('/run/deploy.env')
keys = {'TELEGRAM_BOT_TOKEN', 'ASK_BASE_URL', 'ASK_API_KEY', 'ASK_MODEL'}
assert set(secrets) == keys and all(secrets.get(k) for k in keys)
assert secrets['TELEGRAM_BOT_TOKEN'].split(':', 1)[0] == '8870763711'
assert not temporary.exists()
shutil.copyfile(source, temporary); temporary.chmod(0o600)
for key, value in secrets.items(): set_key(temporary, key, value)
for key, value in {'GITHUB_APP_SLUG':'compadre-assistant',
                   'ENABLED_CAPABILITIES':'obsidian,ask',
                   'ENABLED_CHANNELS':'telegram,vk', 'APP_DEBUG':'false',
                   'DATABASE_PATH':'data/app.db'}.items():
    set_key(temporary, key, value)
before = dotenv_values(source); after = dotenv_values(temporary)
for key in ('VK_BOT_TOKEN','VK_GROUP_ID','GITHUB_APP_ID','GITHUB_CLIENT_ID',
            'GITHUB_PRIVATE_KEY_PATH','TELEGRAM_PROXY_URL'):
    assert before.get(key) == after.get(key), key
assert after['VK_GROUP_ID'] == '240322403'
stat = source.stat(); os.chown(temporary, stat.st_uid, stat.st_gid)
os.replace(temporary, source)
print('Production profile updated; protected settings preserved')
PY
sudo tee /etc/systemd/system/obs-chat-bot.service.d/channels.conf >/dev/null <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/bin/docker compose up -d vk_assistant tg_assistant
ExecStop=
ExecStop=/usr/bin/docker compose stop vk_assistant tg_assistant
EOF
sudo systemctl daemon-reload
systemctl cat obs-chat-bot.service
```

PEM и его путь не меняются. Не запускать сервис до очистки SQLite.

## 4. Создать чистую SQLite и запустить каналы

Только после backup, согласованного окна и остановки всех writers:

```bash
test -f "$rollback_dir/app-before.db"
test -f /opt/obs-chat-bot/data/github-app.pem
rm -f -- /opt/obs-chat-bot/data/app.db /opt/obs-chat-bot/data/app.db-wal /opt/obs-chat-bot/data/app.db-shm
docker compose run --rm --no-deps tg_assistant python -m compadre_assistant --healthcheck --channels telegram,vk
sudo systemctl start obs-chat-bot.service
docker compose ps
```

Healthcheck проверяет конфигурацию/SQLite, но не заменяет запросы к внешним API.
Убедиться, что работают только два новых контейнера, нет старого VK/TG polling
и автоматических рестартов. Старые остановленные контейнеры оставить до приёмки.

```bash
docker compose ps -q | xargs -r docker inspect --format '{{.Name}} {{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}} restarts={{.RestartCount}} restart={{.HostConfig.RestartPolicy.Name}}'
docker compose run --rm --no-deps tg_assistant python -m compadre_assistant --healthcheck --channels telegram,vk
```

Дождаться `healthy`, подтвердить новый Telegram ID и VK Long Poll по безопасной
диагностике. Не публиковать полные logs, inspect или `.env`: они могут содержать
личные сообщения/секреты. Проверить `getMe` через штатный Telegram proxy с токеном
из окружения процесса, выводить только ID/name/username; не помещать токен в CLI.

## 5. Приёмка и завершение

Пользователь последовательно проверяет в новом Telegram и прежнем VK:

1. `/start`, `/register` с именем, `/help`; повторная регистрация после очистки.
2. Привязка каналов по подсказкам `/help`: один пользователь, отдельные режимы
   каждого чата. Режим `/obsidian` в одном канале не переключает другой.
3. `/ask` с коротким вопросом в каждом канале: предварительное подтверждение,
   затем реальный ответ API; следующий вопрос не получает историю предыдущего.
4. `/obsidian`, GitHub Device Flow, выбор тестового vault, проверка installation
   через новый slug, синхронизация. `/exit` возвращает общий режим.
5. Статья → предложение → явное подтверждение → проверка записи в тестовом
   vault. Проверить также отмену предложения без записи. Не подтверждать
   изменение пользовательского vault от имени пользователя.
6. Контролируемый `sudo systemctl restart obs-chat-bot.service`; оба канала снова
   healthy, регистрация/привязка/режимы сохранены, повторного polling нет.

После приёмки: запустить `/usr/local/lib/obs-chat-bot/backup_sqlite.py`, проверить
успешный integrity, затем `sudo systemctl start obs-chat-bot-backup.timer` и
`systemctl list-timers obs-chat-bot-backup.timer`. Backup service и пути менять
не требуется. Удалить только staging `/home/user/.compadre-deploy/secrets.env`;
rollback bundle оставить с правами `700`/`600` до окончания согласованного окна
возврата, не отдавать его в Git. Обновить production snapshot, HISTORY и roadmap
фактическими результатами. Отключение прежнего polling уже сделано остановкой
старых контейнеров; удаление старого бота и рассылка не входят в процедуру.

## Rollback

При провале старта/приёмки прекратить запись новой версии и согласовать возврат
на прежний Telegram с backup: новые регистрации и действия после снимка потеряются.
Если новая версия уже записывала в GitHub vault, возврат SQLite **не отменяет**
эти коммиты; сверить их отдельно, не откатывать пользовательские записи автоматически.

В той же сессии, с сохранённым `rollback_dir`:

```bash
sudo systemctl stop obs-chat-bot-backup.timer
sudo systemctl stop obs-chat-bot-backup.service
sudo systemctl stop obs-chat-bot.service
docker compose stop tg_assistant vk_assistant
test -z "$(docker compose ps --status running -q)"
git status --short
git switch --detach "$(cat "$rollback_dir/old-sha")"
git remote set-url origin "$(cat "$rollback_dir/old-origin")"
cp -p "$rollback_dir/old.env" /opt/obs-chat-bot/.env
rm -f -- /opt/obs-chat-bot/data/app.db /opt/obs-chat-bot/data/app.db-wal /opt/obs-chat-bot/data/app.db-shm
cp -p "$rollback_dir/app-before.db" /opt/obs-chat-bot/data/app.db
sudo cp "$rollback_dir/channels.conf" /etc/systemd/system/obs-chat-bot.service.d/channels.conf
sudo chown root:root /etc/systemd/system/obs-chat-bot.service.d/channels.conf
sudo chmod 644 /etc/systemd/system/obs-chat-bot.service.d/channels.conf
sudo systemctl daemon-reload
while IFS= read -r image_tag; do
  test -n "$image_tag"
  docker tag compadre-assistant:rollback "$image_tag"
done < "$rollback_dir/old-image-tags"
```

До запуска сверить image старого Compose через `docker compose images`:
сохранённые tags восстановлены из `compadre-assistant:rollback`.
Запустить `sudo systemctl start obs-chat-bot.service`, проверить оба прежних
контейнера и старый healthcheck `python -m obs_chat_bot --healthcheck`.
Проверить ответ старого Telegram и VK, только затем включить backup timer.
Если checkout раньше был на ветке, вернуть её лишь после проверки, что её HEAD
совпадает с сохранённым old SHA. Не использовать `git reset --hard`.
Публичные новые имена VK/GitHub/App при rollback сохраняются; старому runtime
нужны проверенные секреты и работоспособный install URL. Если старый slug
перестал перенаправляться, установить `GITHUB_APP_SLUG=compadre-assistant`
отдельной точечной правкой восстановленного `.env` перед запуском.
