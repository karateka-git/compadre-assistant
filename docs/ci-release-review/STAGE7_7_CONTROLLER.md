# Шаг 7.7: отдельная установка controller snapshot

06.11.2026 установлен только проверенный controller snapshot для опубликованной
[v3.2.0](STAGE7_7_RELEASE.md). Это подготовка к настоящему `deploy-prod`,
а не установка runtime. Правила и порядок возврата —
[OPERATIONS.md](../OPERATIONS.md#обновить-отдельно-установленный-controller-snapshot).

## Сверить границу до установки

Read-only осмотр в 18:04:25 UTC подтвердил чистый detached checkout
`dd71c2ba42a0744b5b654cedcf73009ce8879058`, `stage=success`,
`last_good` того же SHA, отсутствие `retry_after`, marker, selector,
`controller.sha` и recovery-blocked. Основной service и backup timer active,
backup service inactive; обе прежние контейнерные ID healthy/restarts=0,
image `sha256:334eeaef6bb4d067ca7fe92dc092ae34362f7f559b8e0f7409351b078402c1b8`.
SQLite только читалась: integrity ok, FK0, schema 1–4, WAL0. Прежний
`state.json` SHA256
`fc0036ce799f6a850b4c0a6702aeda711870aea15456e7ede03b7c284f6ad317`.
Существующий inode `deploy.lock` — `279276`.

Private archive
`/var/lib/compadre-assistant-stage7-20261106/admission-cabc0e2-20261106T160953Z/candidate.tar`
имеет SHA256 `578f10a40946ae5f55e0ebb1c3f6458a8b7e20e18b8bccbb09bb347bcdbb9aca`.
Перед установкой `inspect_archive`, `verify_directory(require_root=True)`
для private control и isolated imports прошли. Локально дополнительно
сверены все **63** файла архива байт-в-байт с `git show
cabc0e24d28947123868db8a60f6b55095feb63e:<path>`; временная
копия архива удалена. Неожиданных links, CR или лишних файлов нет.

## Установить и проверить

Под `sudo flock -n` на **существующем** `deploy.lock` повторены stage,
marker, checkout, service/timer, backup inactivity, точные контейнерные ID,
image/health, archive и manifest. Новый root-owned staging создан через
`controller_snapshot.unpack` и проверен по manifest, mode и isolated imports.
Прежний каталог сохранён переименованием в
`/usr/local/lib/compadre-assistant-deploy.saved-dd71c2b-20261106T180701Z`;
прежний wrapper скопирован в
`/usr/local/sbin/deploy-prod.saved-dd71c2b-20261106T180701Z`.
Прежние inventory SHA256
`88aa3849796ef3c4c21cb1dc3d8e2397fd7a838583a13aadec9773b62beb277f`
и wrapper SHA256
`ee15dac7318d09449c87aa9587e5d4f667801aa082dee0899564ac41c48d4bf8`
повторно совпали с сохранёнными копиями.

Staging переключён двумя rename в том же filesystem; после этого установленный
каталог вновь проверен по exact archive/manifest и isolated imports.
Wrapper root:root `0755` имеет SHA256
`7b6ac5507d8eefb82e2c0ad4b4fe17c09dc452aec53382f4145e407ef447537b`.
Внешний `/var/lib/compadre-assistant-deploy/controller.sha` атомарно записан
root:root `0600` с exact SHA
`cabc0e24d28947123868db8a60f6b55095feb63e` и fsync файла/каталога.
Проверка под lock завершилась **18:07:02.853721 UTC** без recovery.
Имена старого snapshot и wrapper сохранены для допустимого возврата до
появления v2 state; автоматического возврата не выполнялось.

Независимый отдельный процесс в **18:07:38.035900 UTC** снова проверил
63 файла установленного каталога (root:root, корень `0700`), imports, wrapper,
pin, исходный state SHA, inode lock, отсутствие marker/selector и прежний
чистый checkout. SQLite integrity ok/FK0/schema 1–4; service/timer active,
backup service inactive, `NeedDaemonReload=no`. На хосте по-прежнему только
два прежних контейнера: Telegram
`2798069b3f96d3a31c9d3a9d7b12e52c3a754fa4d5348798963209a38869fe42`
и VK `31a165fee7911fc6f51690956954c4d6258e433c1f8a9c768295a3cdf6a15a00`,
оба **healthy**, restarts=0, тот же old image. Их не останавливали и не
пересоздавали. Проверка живых ответов здесь не выполнялась.

## Остановиться до следующего review

Selector drop-ins, GitHub token/policy, release checkout, runtime image,
production БД, backup, marker и controller state не менялись. `deploy-prod`
не запускался. Следующий подэтап — отдельная подготовка private pin/token и
effective units с новым VPS baseline; лишь после их проверки допустим
настоящий выпуск. Отдельный read-only GitHub token пока отсутствует.
