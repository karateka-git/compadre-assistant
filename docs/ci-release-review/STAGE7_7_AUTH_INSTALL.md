# Шаг 7.7: установить исправление авторизации selector

Этот протокол фиксирует отдельную замену установленного controller snapshot на
`aeeb73c557face466ce411b8c22a4aad04750861` после
[отказа допуска из-за публичного лимита GitHub API](STAGE7_7_DEPLOY_INCIDENT.md).
Причина и тесты исправления приведены в [отчёте](STAGE7_7_AUTH_FIX.md),
последнее состояние VPS — в [production снимке](../PRODUCTION.md). Команда
forward recovery, обычный deploy, systemd units и контейнеры не запускались.

## Проверить исходное состояние

06.11.2026 перед записью прочитан `docs/PRODUCTION.md`. Под существующим
`deploy.lock` подтверждены `degraded`, происхождение `finalization_intent`,
admission=true, selector NEW, marker `critical`, отсутствие миграции, оба точных
candidate ID `exited`/restart=no и отсутствие других Docker containers.
Основной service оставался `failed`, backup service и timer — inactive,
timer — disabled. `switch.read_selected` подтвердил идентичность обеих SQLite,
целостность, FK и схему без вывода строк.

Прежний установленный controller pin был
`38be03d75293cbf516456fe45ead9bc17fedcc8d`. Внешний production policy pin
оставался `eb1dad4fee8a176ec4870db12da5e1051a3d1c6e`; приватный GitHub
token проверен только по типу файла, владельцу и режиму root:root/0600.
Main/backup selector drop-ins имели SHA256
`f8a43e2618c91db2e9f54f00525c78197e9cbc8d52d94703b325b51a70be0b8e`
и `57efd9fc8417a8a3871c1826e88647027516067c6a01ee7df640239337504bab`.
Wrapper имел SHA256
`7b6ac5507d8eefb82e2c0ad4b4fe17c09dc452aec53382f4145e407ef447537b`.
Исходный manifest и root ownership старого snapshot проверены.

Новый SHA прошёл полный [main-full 42284495337/1](https://github.com/karateka-git/compadre-assistant/actions/runs/42284495337)
с четырьмя успешными jobs. Закрытый archive
`/var/lib/compadre-assistant-stage7-20261106/controller-aeeb73c-proof/archive.tar.gz`
имел SHA256
`b193a91a7ed40c571b951f2832edbd7f7b4f8a1e780d643769c4a791dbfa091c`.
Проверены manifest, 64 payload файла, root ownership, staging на том же
filesystem и изолированные imports. До замены `require_infrastructure` прошёл.

## Заменить только snapshot

Под тем же lock старый каталог переименован в
`/usr/local/lib/compadre-assistant-deploy.saved-38be03d-20261106T201039Z`.
Wrapper, pin и оба unit drop-ins сохранены в закрытом каталоге
`/var/lib/compadre-assistant-stage7-20261106/controller-aeeb73c-proof/preinstall-20261106T201039Z`.
Проверенный staging атомарно переименован в установленный каталог, затем
синхронизированы каталоги и `controller.sha` атомарно записан с fsync.
Повторная проверка manifest/root ownership/imports прошла.

## Независимо проверить результат

Отдельный процесс из `/opt/obs-chat-bot` с `python3 -P` импортировал
`scripts.deploy_prod` из установленного snapshot. Pin стал
`aeeb73c557face466ce411b8c22a4aad04750861`, 64 payload файла прошли
проверку; `require_infrastructure` снова прошёл. Wrapper и оба drop-ins
сохранили прежние SHA256. Хэши `state.json`, selector и обеих SQLite до и после
замены совпали; marker остался `critical`, selector — NEW, оба точных контейнера
остались остановленными, service — `failed`, timer — inactive. Production
policy и token не менялись.

Это завершает только подготовку controller. Следующий шаг — свежий независимый
допуск и отдельное решение о единственном root-only `deploy_forward_recovery`
на выбранной NEW DB. Пока восстановление не подтверждено, оба канала
недоступны, а выпуск v3.2.0 нельзя считать установленным.
