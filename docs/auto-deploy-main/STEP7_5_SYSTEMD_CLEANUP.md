# Шаг 7.5: удалить устаревший systemd drop-in

Статус на 20.10.2026: очистка выполнена в отдельно разрешённом окне
06:09:03–06:10:24 UTC; `WINDOW_OK`, exit 0. Файл 90 отсутствует, reload и
один restart прошли успешно. Независимый read-back — 06:10:55 UTC.
Весь шаг 7.5 остаётся открытым до проверки обновлённого fan-out в GitHub.

## Фактическое окно и сохранённые копии

Сценарий SHA256 `3ef7a16ad2fce045b7f759eb9b877a0ed68cbb031028dbeea3cd3e12fa31bcfe`
перед unlink повторил preflight под существующим deploy.lock и сохранил
проверенные drop-in, приватный pin/state и снимок effective systemd в
`/var/lib/compadre-assistant-deploy/maintenance-remove90-7e61b6fz` (root:root
0700, приватные файлы 0600). После удаления изменился только набор drop-in:
загружены channels/zz, 90 отсутствует. Host.installed подтвердил команды,
privilege flags, EnvironmentFiles/Conditions, root-private pin и Compose;
после restart Host.verify_both подтвердил health/revision/polling.

Независимая проверка: service active/enabled, Result success,
NeedDaemonReload=no, ActiveEnterTimestamp 06:09:23 UTC; оба healthy,
restarts 0, unless-stopped на прежнем image/SHA v1.0.4; duplicate Docker
polling отсутствует. State/pin побайтово прежние, marker отсутствует.
SQLite inode 501886, integrity ok, схема 0001; timer active/enabled,
следующий запуск 21.10 03:15 UTC. Окно с ночной копией не пересекалось.

Первая попытка 06:07:11–06:07:18 UTC завершилась до restart и вернула
90 (`ROLLBACK_OK`): одинаковые записи EnvironmentFiles/Conditions от 90 и zz
ошибочно сравнивались как разные списки. Read-back 06:07:45 UTC подтвердил
полный возврат. Её bundle `maintenance-remove90-j9jmxvj4` также сохранён.
После исправления только семантики этих двух полей, четырёх локальных
проверок, успешного preflight и повторного ревью выполнена вторая попытка.
ExecStartEx/ExecStopEx и остальные поля проверялись строго.

Незагружаемый файл `zz-deploy-pinned.v101-saved-d3fa4efa` не изменялся:
набор загружаемых drop-in проверялся только среди `*.conf`, отдельно от
исторических копий. Срок удаления maintenance bundles не согласован.
Ниже сохранены исторический preflight и проверенный порядок окна/возврата;
повторять удаление и restart для уже выполненной очистки не требуется.

## Допуск контроллера

Установленный `v1.0.4` уже допускает отсутствие
`/etc/systemd/system/obs-chat-bot.service.d/90-deploy-pinned.conf`.
`Host.require_systemd_boot` ожидает `channels.conf`, затем
`zz-deploy-pinned.conf`; старый файл добавляется в ожидаемый список только
при наличии. `Host.installed` проверяет его байты только при наличии.
Новый runtime-код для удаления не требуется.

Расширен `SystemdBootTests.test_installed_accepts_only_checked_legacy_and_late_files`:
старый проверенный файл допускается, изменённый отвергается, отсутствие
файла при старом загруженном `DropInPaths` отвергается, после обновления
списка unit проходит полную проверку `installed`. Остальные проверки
effective команд, root privilege flags, EnvironmentFile, marker guard,
приватного pin и неизвестных drop-in сохраняются.

## Исторические read-only факты VPS (19.10.2026)

- Checkout чистый, SHA `d6b7d9267e7754ee90957d9057e2a4eb41f789c1`.
- Service active/enabled, `NeedDaemonReload=no`; порядок: 90, channels, zz.
- Оба канала running/healthy, restart count 0, policy unless-stopped;
  общий image ID
  `sha256:434ab2d8860bee4dfddfeb7311973813ca3f41e47ede81d509525e973f2aba3d`.
- State success, last_good v1.0.4 с тем же SHA, marker отсутствует.
- Backup timer active/enabled, следующий запуск 20.10.2026 03:15 UTC.
- Runtime/обычный и critical Compose override: root:root, 0600;
  `Host.installed(image_id, sha)` установленного контроллера успешно прошёл
  read-only проверку effective systemd, приватного pin, Compose и образа.
- Все три drop-in root:root 0644. SHA256:

```text
90-deploy-pinned.conf d9d9bbbfeb5f7614fe250c91ef6adeb301fcc8eed443c165ffc829f46e0824e3
channels.conf         9369733bdde35f37d796e2fa8cf69270f59a5c541427e604183d2e2470516e65
zz-deploy-pinned.conf ef4f911ef49c96f1b9d7bba1698544e3f8a4332125bbbee7ecf88324906369c4
```

Чтение выполнялось по SSH с ключом из PRODUCTION.md, через
`sudo -n python3 -B -`: без записи pycache, вывода .env/PEM/ключей/полного
inspect и без изменений файлов/служб. Факты необходимо перечитать в начале окна.

## План отдельного окна

1. Прочитать PRODUCTION.md и согласовать перезапуск обоих каналов. Под общим
   `/var/lib/compadre-assistant-deploy/deploy.lock` повторить read-only preflight;
   при marker, незавершённом state, нездоровом канале или изменении pin
   остановиться. Не совмещать окно с ночным backup.
2. Создать root:root 0700 maintenance-каталог с уникальной датой за пределами
   `obs-chat-bot.service.d`. Сохранить туда все три drop-in с владельцем и
   режимом, безопасный снимок effective свойств, SHA/image ID, state и backup
   timer. Приватные runtime/Compose при сохранении остаются 0600; содержимое
   не выводить. Побайтово сверить копии и зафиксировать путь возврата.
3. Удалить **только** точный regular file
   `/etc/systemd/system/obs-chat-bot.service.d/90-deploy-pinned.conf`, предварительно
   сверив отсутствие symlink и сохранённый SHA256. Выполнить
   `sudo systemctl daemon-reload`; контейнеры пока продолжают работать.
4. Проверить `systemd-analyze verify` трёх units, `NeedDaemonReload=no`, ровно
   `channels.conf` и `zz-deploy-pinned.conf` в DropInPaths, effective
   ExecStartEx/ExecStopEx и privilege flags, EnvironmentFiles и marker
   Conditions. Вновь вызвать `Host.installed` с прежними SHA/image ID:
   он также проверит private pin, Compose и startup guards. При отказе
   вернуть файл и reload **до** перезапуска.
5. Один раз выполнить `sudo systemctl restart obs-chat-bot.service`. Проверить
   active/enabled service, оба running/healthy на прежнем image ID и SHA,
   штатный polling Telegram/VK без дублей, policy unless-stopped,
   отсутствие marker, прежний state и active/enabled backup timer. Использовать
   `Host.verify_both` и `require_no_duplicate_polling`, выводить лишь итоги.
   Проверить SQLite read-only integrity и сохранённую схему без вывода данных.
6. При успехе записать факт отсутствия 90, effective конфигурацию и результаты
   в PRODUCTION/HISTORY. Это не выпуск новой версии и не меняет runtime pin.
   Копию конфигурации сохранить до завершения приёмки; срок удаления отдельно.

## Возврат при отказе

В том же согласованном окне вернуть только сохранённый 90 из проверенной
копии через `install -o root -g root -m 0644`, выполнить daemon-reload и
сверить прежние три DropInPaths и effective команды. При необходимости
повторно запустить службу на **том же** закреплённом образе; подтвердить оба
канала, polling, service и timer. SQLite, runtime.env, checkout, образы и
другие drop-in не заменять. Если безопасный запуск не подтверждается,
остановиться, сохранить факты и применить отдельный recovery из OPERATIONS;
не объявлять приёмку успешной и не пытаться восстанавливать БД автоматически.

Удаление 90 и контролируемый restart выполнены 20.10.2026. GitHub-приёмка
обновлённого fan-out остаётся отдельным незавершённым условием шага 7.5.
