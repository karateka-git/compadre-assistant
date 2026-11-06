# Штатный выпуск v3.2.0: отказ при возврате systemd unit

Этот документ фиксирует наблюдаемое состояние production VPS после единственного запуска `sudo deploy-prod v3.2.0` 06.11.2026 и проверяемую причину отказа. Порядок восстановления описан в [операционной инструкции](../OPERATIONS.md#отказ-выпуска-и-возврат); завершение шага 7.7 остаётся открытым в [плане](PLAN.md). Это запись о факте, а не разрешение повторить выпуск или вручную изменить state.

## Наблюдение

Перед выпуском exact Release `v3.2.0` указывал на `cabc0e24d28947123868db8a60f6b55095feb63e`, полный CI `42249037867/1` и artifact `13519278785` были подтверждены для предыдущего установленного SHA `dd71c2ba42a0744b5b654cedcf73009ce8879058`. Контроллер `09f15a5b3c01ec73d02379b918104e8cf697b4b2` и selector units прошли отдельный инфраструктурный допуск. До остановки обоих каналов SQLite имела `integrity_check=ok`, ноль нарушений FK и миграции 1–4; другие writers не обнаружены.

Команда началась в 19:07:36 UTC. Проверка candidate после `candidate_started` прошла, но `systemctl start` в стадии `finalization_intent` завершился отказом helper. Попытка автоматического возврата также не подтвердила SQLite recovery, и в 19:10:29 UTC команда завершилась с кодом 1 и `stage=degraded`. Контроллер оставил защитный marker и остановил timer и оба контейнера. Метрика зафиксировала `downtime_seconds=73.271`, `downtime_open=true`; это время до отказа, а не время восстановленного сервиса.

На последнем read-only осмотре selector выбирал новое поколение `app-212d6cd591814c999c01876d032119f6.db`, marker существовал, основной unit был `failed`, backup timer `inactive`. Candidate TG `d64df2110b755ec72701fbdf81490815ea3423fcdd8c8abadb5137aae8f20d8e` и VK `2e423cb1b1ed7589f65fa2d326b542dbe007edba207cef7094dedbf0816efbdc` были `exited`, с `restart=no`. Других channel ID в Docker inventory не было. Прежняя и выбранная новая SQLite обе прошли integrity/FK, сохранили миграции 1–4, одинаковый fingerprint схемы `11e5bb7311a50c33e8cf5f7968c8b72ad54dfae0136dc02bbccba15367c3fd15` и тот же logical digest, что до выпуска: `49103ef442d3119678762c39c9707c555a10f7670a4d4cb7218a581c05ca50c1`. Содержимое строк не выводилось.

## Причина и исправление

Effective `WorkingDirectory` основного unit — `/opt/obs-chat-bot`, а selector helper запускался как `python3 -B -m scripts.deploy_runtime` с `PYTHONPATH=/usr/local/lib/compadre-assistant-deploy`. Python ставил текущий каталог впереди `PYTHONPATH` и импортировал `scripts` из checkout выпуска. В нём отсутствует `controller-manifest.json`; read-only повтор `Host.require_switch_rollout()` из того же каталога дал `FileNotFoundError` этого файла. Из `/` тот же import выбирал установленный snapshot. Команда `python3 -P -B -m ...` из checkout на VPS выбрала root-owned snapshot; VPS использует Python 3.14.4. Проверка Docker inventory отвергла альтернативную гипотезу о старых контейнерах с чужим writable mount.

Изменение добавляет `-P` к точным main start/stop и backup helper командам, сохраняя независимый root-owned `PYTHONPATH`. Контракт D-Bus продолжает сверять полный argv, поэтому unit без `-P` не пройдёт gate. Изменённые шаблоны требуют нового reviewed controller snapshot и отдельной установки; published runtime Release `v3.2.0` остаётся прежним.

## До восстановления

Оставить `degraded`, marker, selector NEW, остановленные ID и timer без изменений. После полного CI исправленного контроллера проверить новый snapshot в закрытом Linux-каталоге и отдельно установить его вместе с exact unit bytes под lock. Затем нужен проверенный forward recovery текущего v2 state на выбранной новой БД: без новой миграции или создания контейнеров, с durable intent, точными ID/image/OCI, повторным evidence, DB integrity/FK/schema и проверкой обоих каналов. Отказ любой проверки требует подтверждения остановки writers и marker; при недоказанной защите нужен ручной разбор. Ручная запись `stage=success` или снятие marker не является восстановлением.

## Подготовленный контроллер и закрытая репетиция

Исправление прошло PR [#71](https://github.com/karateka-git/compadre-assistant/pull/71)
и дополнительный fail-closed review PR
[#72](https://github.com/karateka-git/compadre-assistant/pull/72). SHA первого merge
`bde6d2636731703f55b4b9a7bdea86cf240e3ff9` **не допущен к установке**:
после его merge обнаружено, что отказ preflight при повторе мог оставить уже
запущенный writer. Исправленный exact merge SHA —
`38be03d75293cbf516456fe45ead9bc17fedcc8d`.

Root-only `scripts.deploy_forward_recovery` допускает только сохранённые
`degraded`/forward стадии исходного перехода с `rollback_origin_stage=finalization_intent`,
`admission=true`, `boot.outcome=success`, selector NEW и `migration_attempted=false`.
Он сверяет прежние ID, image/OCI, SQLite identity/integrity/FK/fingerprint и
точное внешнее Release/CI evidence. Перед start записывает durable forward
intent; повтор использует те же ID без `compose create` и новой миграции.
После отказа проверяет отдельно marker, timer, `degraded` и остановку каждого
известного ID. Если Docker физически не остановил writer, сообщение явно
говорит, что остановка или guards **не доказаны**.

Локально через проектный venv выполнены 67 целевых `unittest` для snapshot,
main/backup systemd, v2 runtime/rollout и recovery: **OK**, один Linux-only
skip. Ручной полный [main-full 42281273724/1](https://github.com/karateka-git/compadre-assistant/actions/runs/42281273724)
завершился **success** на exact SHA `38be03d75293cbf516456fe45ead9bc17fedcc8d`:
controller, `repository-compatibility`, `full-suite` и `ci-metrics` успешны.
Repository policy pin оставался `09f15a5b3c01ec73d02379b918104e8cf697b4b2`
для установленной перед выпуском базы v3.1.0; production external pin
`eb1dad4fee8a176ec4870db12da5e1051a3d1c6e` для runtime v3.2.0 не менялся.

Из exact merge SHA создан controller archive SHA256
`2db58a00e8a34ae99cd16ce7f0e991e7c9cba72fe61699083212e9f7f4023ede`:
64 файла. Закрытый Linux archive
`/var/lib/compadre-assistant-stage7-20261106/controller-38be03d-proof/archive.tar.gz`
и `staging` в том же каталоге под `root:root` подтвердили digest, manifest,
root ownership и изолированные imports. Архив **не установлен** в production
controller path.

В `rehearsal-v1` того же закрытого каталога взята копия фактического degraded
state/selector и обеих SQLite. Пути, device и inode поколений пересчитаны
только в копии; fingerprints совпали. Fake transport не обращался к реальным
Docker, systemd или GitHub и не содержал credentials каналов. На реальном
Linux проверены исключение второго владельца `deploy.lock`, durable intent до
fake start, использование тех же ID, selector NEW, чтение SQLite integrity/FK
и финализация до `success` в копии. SHA256 исходных production state, selector
и двух файлов SQLite до и после опыта совпали. Проверка Git checkout, сетевого
evidence, реального health/polling и systemd в репетиции была изолирована;
перед production recovery их нужно подтвердить отдельно.

На последнем VPS осмотре production по-прежнему `degraded`, marker присутствует,
backup timer выключен, оба candidate ID остановлены, установленный controller
остаётся `09f15a5b3c01ec73d02379b918104e8cf697b4b2`. Следующий шаг —
отдельная проверка и установка нового controller snapshot и exact selector
units под lock; затем свежий допуск и согласованный forward recovery.
