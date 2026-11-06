# Шаг 7.7: подготовить production pins и selector units

Этот протокол фиксирует отдельную установку инфраструктуры для штатного
выпуска v3.2.0. Controller snapshot уже установлен и проверен в
[предыдущем подэтапе](STAGE7_7_CONTROLLER_UPDATE.md); порядок эксплуатации —
в [OPERATIONS.md](../OPERATIONS.md#подготовить-backup-и-штатный-запуск-для-sqlite-v2).
`deploy-prod` здесь не запускался, боты и рабочая БД не переключались.

## Повторно проверить исходное состояние

06.11.2026 в 19:00:20 UTC read-only baseline подтвердил чистый checkout
`dd71c2ba42a0744b5b654cedcf73009ce8879058`, установленный controller
`09f15a5b3c01ec73d02379b918104e8cf697b4b2`, исходный `state.json`
SHA256 `fc0036ce799f6a850b4c0a6702aeda711870aea15456e7ede03b7c284f6ad317`
и inode `deploy.lock=279276`. `stage=success`, `last_good=v3.1.0`;
marker, selector и cooldown отсутствовали. Service и backup timer active,
backup service inactive; Telegram и VK имели прежние exact IDs и image,
оба `healthy`, `restarts=0`. SQLite только читалась: integrity ok, FK0,
миграции 1–4. Старый основной drop-in SHA256
`ef4f911ef49c96f1b9d7bba1698544e3f8a4332125bbbee7ecf88324906369c4`;
backup selector и private token/policy отсутствовали.

## Установить под существующим lock

Под `flock -n` на прежнем `deploy.lock` повторены stage, marker, pin,
systemd, checkout, БД и оба exact container ID. Прежний основной drop-in
сохранён в root-owned закрытом каталоге как
`/var/lib/compadre-assistant-stage7-20261106/infra-old-main-20261106T190131Z.conf`;
его SHA256 повторно совпал с исходным. Затем атомарно записаны exact байты
из установленного controller snapshot:

| Файл | SHA256 | Права |
| --- | --- | --- |
| `/etc/systemd/system/obs-chat-bot.service.d/zz-deploy-pinned.conf` | `5aa88b4fb33bf62629713f925a93d91b21d513ec513ecebd930f7b189e14ff0b` | root:root/0644 |
| `/etc/systemd/system/obs-chat-bot-backup.service.d/zz-deploy-selector.conf` | `d89f049a9adee744b021360392e4c528440d5650e2faa3bf8ef41ac06dd942d3` | root:root/0644 |

Отдельный fine-grained GitHub read-only token передан из пользовательского
локального файла только по SSH stdin; его значение и digest не выводились,
не передавались в argv и не сохранялись в отчёте. Private файл
`github-read-token` и внешний production `compatibility-policy.sha` имеют
root:root/0600. Production pin равен
`eb1dad4fee8a176ec4870db12da5e1051a3d1c6e`: это reviewed policy
для **установленной v3.1.0 → опубликованной v3.2.0**. Repository CI variable
`COMPATIBILITY_POLICY_SHA=09f15a5...` относится к следующим CI runs и не
подменяет production pin текущего выпуска. Временных token файлов не осталось.

После `daemon-reload` установленный controller выполнил
`deploy_rollout.require_infrastructure(Host())` без обхода. Этот read-only
gate проверил байты двух selector drop-in и marker guards,
`systemd-analyze verify`, `NeedDaemonReload=no`, порядок drop-ins,
effective D-Bus `ExecStartEx`/`ExecStopEx` и `no-setuid`, условия marker,
`RemainAfterExit=true`, backup helper и отсутствие известных сторонних
Docker writers. Установленные units при этом не запускали и не перезапускали.

## Независимо проверить результат

В отдельном SSH/Python процессе gate повторно завершился успешно.
GitHub API с установленным private token ответил HTTP 200 для Actions и
Contents; тело ответа и token не выводились. Исходный state SHA256,
lock inode и чистый checkout остались прежними; marker/selector отсутствуют.
Production SQLite: integrity ok, FK0, миграции 1–4. Telegram и VK имеют
прежние точные IDs и old image, `healthy`, `restarts=0`; service/timer active,
backup service inactive, `NeedDaemonReload=no`. Вспомогательный скрипт
установки удалён из закрытого каталога; временных token файлов нет.

Следующий шаг требует отдельного review и свежего admission точного Release,
CI run/attempt, artifact, image и backup. Только после него допустим
штатный `deploy-prod v3.2.0`. Живые ответы каналов ещё не проверялись.
