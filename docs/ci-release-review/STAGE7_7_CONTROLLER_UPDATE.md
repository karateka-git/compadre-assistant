# Шаг 7.7: отдельное обновление установленного controller snapshot

Этот протокол фиксирует замену только отдельного контроллера на VPS.
Процедура и восстановление описаны в
[OPERATIONS.md](../OPERATIONS.md#обновить-отдельно-установленный-controller-snapshot),
допуск исправления и базы CI — в
[протоколе CI](STAGE7_7_CI_BASELINE.md). Установка runtime v3.2.0 и
`deploy-prod` в этом действии не выполнялись.

## Проверить исходное состояние

06.11.2026 перед записью VPS имел чистый detached checkout
`dd71c2ba42a0744b5b654cedcf73009ce8879058`, `stage=success`,
`last_good=v3.1.0`; `retry_after`, marker и selector отсутствовали.
Исходный `state.json` SHA256
`fc0036ce799f6a850b4c0a6702aeda711870aea15456e7ede03b7c284f6ad317`,
inode существующего `deploy.lock=279276`. Основной service и backup timer
active, backup service inactive. Оба прежних container ID и old image
сохраняли `healthy`, `restarts=0`; рабочих backup файлов было пять.
Production SQLite проверена только на чтение: integrity ok, FK0,
миграции 1–4. Установленный controller pin указывал на
`cabc0e24d28947123868db8a60f6b55095feb63e`.

Новый controller SHA `09f15a5b3c01ec73d02379b918104e8cf697b4b2`
получил успешный полный [main-full 42273239857/1](https://github.com/karateka-git/compadre-assistant/actions/runs/42273239857).
Закрытый архив в
`/var/lib/compadre-assistant-stage7-20261106/controller-09f15a5-proof/candidate.tar`
имел SHA256
`65a024d790a189344be06b4f5a33432b212dcab280b00a3ac72b0bcfce6d1dd3`.
До установки его manifest, 63 файла, root права и изолированные imports
прошли private Linux проверку. Прежний архив `cabc0e2` имел проверенный
SHA256 `578f10a40946ae5f55e0ebb1c3f6458a8b7e20e18b8bccbb09bb347bcdbb9aca`.

## Заменить snapshot под lock

Под `flock -n` на **том же inode** повторены state, marker, checkout,
service/timer, отсутствие backup и состояние обоих контейнеров и БД.
Прежний установленный каталог повторно проверен по архиву `cabc0e2`.
Старый и новый wrapper имели одинаковый SHA256
`7b6ac5507d8eefb82e2c0ad4b4fe17c09dc452aec53382f4145e407ef447537b`;
wrapper сохранён, но его байты менять не потребовалось. Прежние controller,
wrapper и pin сохранены как отдельные копии:

- `/usr/local/lib/compadre-assistant-deploy.saved-cabc0e2-20261106T185354Z`;
- `/usr/local/sbin/deploy-prod.saved-cabc0e2-20261106T185354Z`;
- `/var/lib/compadre-assistant-deploy/controller.sha.saved-cabc0e2-20261106T185354Z`.

Два rename в одной файловой системе переключили каталог на проверенный
private staging; каталог синхронизирован. После замены `verify --require-root`
и isolated imports снова подтвердили 63 файла. `controller.sha` записан
атомарно root:root/0600 с fsync файла и каталога на exact новый SHA.
Процедура завершилась без recovery и без вызова `deploy-prod`.

## Независимо проверить результат

Отдельный процесс проверил установленный архив/manifest, 63 файла,
root:root/0700, imports, pin root:root/0600 и неизменный wrapper.
Сохранённый `cabc0e2` каталог также прошёл `verify --require-root` по
старому архиву; копия pin содержит прежний точный SHA. Кратковременный
обрыв SSH при первом postcheck не изменил серверное состояние: повторные
независимые проверки завершились успешно.

Исходный state SHA256 и lock inode остались прежними, marker и selector
отсутствуют; checkout по-прежнему чистый на `dd71c2b`. Production SQLite:
integrity ok, FK0, миграции 1–4; backup файлов по-прежнему пять.
Telegram и VK сохранили прежние точные container IDs и old image,
`healthy`, `restarts=0`. Service/timer active, backup service inactive,
`NeedDaemonReload=no`. Старый main drop-in SHA256
`ef4f911ef49c96f1b9d7bba1698544e3f8a4332125bbbee7ecf88324906369c4`
не изменился; backup selector, private GitHub token и production policy pin
отсутствуют. Живые ответы каналов этим подэтапом не проверялись.

Следующий переход требует отдельной проверки и review: установить private
read-only token, production policy pin для опубликованной пары
`v3.1.0 → v3.2.0` и selector units, затем проверить effective systemd.
До этого настоящий выпуск `v3.2.0` не запускался.
