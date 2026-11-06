# Шаг 7.7: установить контроллер для восстановления

Этот протокол фиксирует только замену controller snapshot и selector units после
[отказа штатного выпуска](STAGE7_7_DEPLOY_INCIDENT.md). Команда forward recovery,
обычный `deploy-prod`, контейнеры и backup timer здесь не запускались. Причина
`degraded` и ограниченный путь восстановления уже разобраны; это отдельное
исключение из общего запрета установки snapshot при неразобранном `degraded`
в [операционной инструкции](../OPERATIONS.md#обновить-отдельно-установленный-controller-snapshot).

## Проверить допуск и исходные файлы

06.11.2026 перед записью VPS прочитан актуальный [production снимок](../PRODUCTION.md).
Новый controller SHA `38be03d75293cbf516456fe45ead9bc17fedcc8d` прошёл
полный [main-full 42281273724/1](https://github.com/karateka-git/compadre-assistant/actions/runs/42281273724):
controller, `repository-compatibility`, `full-suite` и `ci-metrics` success.
Закрытый archive
`/var/lib/compadre-assistant-stage7-20261106/controller-38be03d-proof/archive.tar.gz`
имел SHA256
`2db58a00e8a34ae99cd16ce7f0e991e7c9cba72fe61699083212e9f7f4023ede`;
его manifest, 64 root-owned файла и изолированные imports заранее прошли
проверку на Linux. Ранее установленный snapshot `09f15a5b3c01ec73d02379b918104e8cf697b4b2`
прошёл собственный manifest/byte check. Старый и новый wrapper имели один
SHA256 `7b6ac5507d8eefb82e2c0ad4b4fe17c09dc452aec53382f4145e407ef447537b`,
поэтому его байты не менялись.

Существующий `deploy.lock` имел inode `279276`. Под тем же lock повторно
подтверждены `stage=degraded`, `rollback_origin_stage=finalization_intent`,
durable admission=true, selector NEW, marker `critical`, `migration_attempted=false`,
точные два остановленных candidate ID с `restart=no`, неактивный backup timer
и backup service. `switch.read_selected` прочитал обе зарегистрированные SQLite
с integrity/FK и точным fingerprint без вывода строк. Других Docker writers
к data не обнаружено. Исходные main/backup drop-in SHA256 были
`5aa88b4fb33bf62629713f925a93d91b21d513ec513ecebd930f7b189e14ff0b`
и `d89f049a9adee744b021360392e4c528440d5650e2faa3bf8ef41ac06dd942d3`.

## Заменить snapshot и units под lock

Под lock прежний snapshot переименован в
`/usr/local/lib/compadre-assistant-deploy.saved-09f15a5-20261106T194944Z`.
Wrapper, `controller.sha` и обе прежние units сохранены в root-private
`/var/lib/compadre-assistant-stage7-20261106/controller-38be03d-proof/preinstall-20261106T194944Z`.
Два rename на одном filesystem device `64769` установили проверенный staging;
каталог синхронизирован. Новый manifest/root ownership/imports подтверждены
повторно. `controller.sha` атомарно записан как root:root/0600 с fsync.
Main и backup drop-ins заменены exact байтами нового snapshot, затем выполнен
только `systemctl daemon-reload` и read-only
`deploy_rollout.require_infrastructure(Host())`. Gate проверил
`systemd-analyze verify`, effective D-Bus argv main start/stop и backup helper
с `-P`, условия marker, порядок units и отсутствие чужих writers.

## Независимо проверить результат

Из отдельного процесса с `WorkingDirectory=/opt/obs-chat-bot` и `python3 -P`
импортированный `SOURCE_ROOT` указывал на установленный snapshot, а не на
release checkout. Повторная проверка archive/manifest/root ownership и
изолированных imports подтвердила 64 файла. Exact SHA256 новых main и backup
drop-ins — `f8a43e2618c91db2e9f54f00525c78197e9cbc8d52d94703b325b51a70be0b8e`
и `57efd9fc8417a8a3871c1826e88647027516067c6a01ee7df640239337504bab`.
`NeedDaemonReload=no`; инфраструктурный gate прошёл повторно.

До и после установки под lock SHA256 `state.json`, selector и обоих файлов
SQLite совпали. После установки state SHA256
`5c95017aa24e1c731d65abe732c03a0ba4d5d3e383b9495628209609c5e36aae`,
selector SHA256 `16d64258d24acd5c69775aed154e0fc288104a5489eef45170408cf32011c936`.
Marker остался `critical`, selector выбирает NEW, оба точных candidate ID
остались `exited`, service `failed`, timer `inactive`; controller pin теперь
`38be03d75293cbf516456fe45ead9bc17fedcc8d`. Release checkout, runtime
image/pin, production policy/token, state и БД не изменяли.

Дальше требуется отдельный review свежего production state, exact внешнего
Release/CI evidence и backup перед единственным root-only
`scripts.deploy_forward_recovery`. Живые ответы Telegram/VK пока не проверены.
