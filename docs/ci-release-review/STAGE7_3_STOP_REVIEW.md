# 7.3: проверить real bootstrap перед единственным stop

**Действует новое явное указание пользователя:** maintenance окно открыто
16:58:07.942252 UTC, оба бота остановлены с16:58:19.824951 UTC и остаются
выключенными до завершения испытаний/7.6. После отказа case не выполнять
автоматический start, кроме угрозы данным/отдельной команды пользователя.
Real03 успешно завершён17:04:37 UTC, независимый review родителя пройден;
[итог7.3 и caveats](STAGE7_3_RESULT.md). Исторические staging hashes и контракт
сохранены [ниже](#подготовить-real03-в-открытом-maintenance-окне).
Ниже сохранена история двух прежних коротких окон, завершённых recovery по
прежнему указанию. Их старый recovery-on-failure протокол теперь не действует.

Актуально на 06.11.2026 16:28 UTC: разрешённое окно real01 прервано на stop
attestation; настоящий case и мигратор не стартовали. Прежний сервис немедленно
восстановлен, оба прежних IDs healthy/polling. Простой по интервалу stop attempt
— verified recovery: **32.235 с**. Исправленный bootstrap пока проверен только
локально и read-only на Linux; real02 staging не создавался, второго stop нет.
Подробности — [Разобрать первое окно](#разобрать-первое-окно).

Ниже сохранён исторический pre-stop review real01. На 06.11.2026 16:20 UTC bootstrap подготовлен и выполнен только в режиме
`prepare()`. Остановка и настоящий case **не выполнялись**. Основные receipts —
[STAGE7_3_NATIVE_PROOF.md](STAGE7_3_NATIVE_PROOF.md); порядок окна —
[PLAN.md](PLAN.md#7-испытать-выпуск-на-копии-и-установить-версию-на-production).

## Проверить код и staging

Код: `stage7_3_real_bootstrap.py`, `stage7_3_harness.py`. Обычного CLI нет;
bootstrap не вызывает stop/start, deploy, checkpoint или production SQLite.
Он предоставляет только reviewed функции `prepare`, `attest_stopped`,
`run_after_stop`. Runtime/controller source в этом изменении не менялся.

Native CI/image/synthetic receipts закреплены константами SHA256 **в коде**,
их hashes не принимаются через config или env. Harness real branch читает
копии этих receipts и проверяет hash и exact config binding. Самозаявленное
full_ci_success=true без reviewed bytes не даёт допуска. Bootstrap дополнительно
сверяет live native verify_evidence до stop (fixed ZIP artifact13519278785;
get/pages штатные), exact source/snapshot и image/OCI обеих версий, свежий
baseline state/checkout/IDs/units/backup/free-space. SQL names берутся только из
build-context.tar с hash из pinned image receipt и сверяются с распакованными
байтами. Проверка tar/control выполняется до imports snapshot.

| Объект | Путь / SHA256 |
| --- | --- |
| Bootstrap на VPS | `admission-cabc0e2-20261106T160953Z/real-bootstrap.py` / `fa88acd2eb16656a81c5e73b2c54f039d59baf3715b8654fe4a332256b4a09cd` |
| Harness | `admission-cabc0e2-20261106T160953Z/harness.py` / `7a4cef267e3132b8e5e786ad2803649e54f6de55a8ff47fd426673ff2286132a` |
| New case root | `/var/lib/compadre-assistant-stage7-20261106/controller-proof-cabc0e2-real01`, root:root0700 |
| prepared.json | `b9a9aaf1e932fbf920cd8b808be0375141529eb307584bf00f4de72720e4ee87` |
| reviewed-config.json | `ed5e219bed48ee43ec4e22f3a5056842ffa8dcebcdb0e888dea3ab0206abe724` |
| CI receipt | `914166c01b089cab426d5f2705b4040c47d334deecc1779b629892f6640fd72d` |
| Image receipt | `42c8fffe8b4753545dfd65244c5100626570661a07b346a9bd6cbcdb97727d4e` |
| Synthetic receipt | `5d0545e18b316635ee558472ee0258beec12a7196821a1575960e1595b5cf69b` |

Все относительные VPS пути таблицы находятся под
`/var/lib/compadre-assistant-stage7-20261106`. Prepare baseline UTC
`2026-11-06T16:20:10.533117+00:00`; последний readonly baseline 16:20:55 UTC
подтвердил прежние running/healthy IDs, restarts0, state hash и active units.
В новом root нет stopped.json, execution-started.json, result.json и scenarios.
Есть только staging code/snapshot/receipts и exact main copy без sidecars.

Локальные проверки explicit project venv: harness **4 tests OK, 2.022 с**;
bootstrap **5 tests OK, 0.036 с**. Проверены отсутствие native receipt,
изменённые bytes и несоответствие image/artifact/schema/fullCI; stop freshness,
restart/units mismatch, отсутствие stopped receipt до любых команд,
one-shot запись, immutable SQLite и отказ изменённого SHM.

## Сохранить исходную fixture

Первый prepare отказал до staging: ожидался mode0600/отсутствие sidecars,
но 7.1 source намеренно root0400, а исходные WAL/SHM сохранены с 13:54 UTC.
Readonly inspect подтвердил main 12677120 bytes, прежний SHA5a7a6a...,
WAL строго0 bytes (SHA e3b0c442...), SHM32768 bytes
(SHA fd4c9fda9cd3f9ae7c962b0ddf37232294d55580e1aa165aa06129b8549389eb).
Это согласуется с исходной записью 7.1 о нулевом WAL.

По согласованному review bootstrap теперь требует exact main/sidecar
size/hash/mtime, root owner и modes0400/0600. Immutable read проверяет
integrityok/FK0/журнал0001–0004 и неизменность всех трёх файлов до/после.
Исходные sidecars не удалялись, checkpoint/backup над source не выполнялся.
В case скопированы только main bytes SHA5a7a6a...; mode0600, sidecars отсутствуют.
Immutable integrity case не создаёт WAL/SHM. При любом изменении source или
непустом WAL допуск снова закрывается; расширять это исключение нельзя.

## Выполнить stop и case только после review

Следующие команды — **протокол для родителя после отдельного go**, сейчас
они не выполнены. До stop независимо сверить hashes таблицы и effective
ExecStop, подтвердить окно без ручных writers, отсутствие backup/deploy,
CI/artifact freshness. Проверить, что prepared baseline не старше15 минут;
при просрочке не останавливать сервис: нужен новый reviewed private root и
новый prepare (существующий staging/receipts не перезаписывать).

1. Записать UTC начала единственного окна в private журнал. Один раз выполнить
   `sudo systemctl stop obs-chat-bot.service`. Effective ExecStop должен
   остановить прежние VK и Telegram; policy unless-stopped не менять.
2. Read-only проверить inactive service, оба exact прежних IDs exited и
   отсутствие других containers/writers. Вызвать отдельно:

   ```python
   import runpy
   proof = runpy.run_path('/var/lib/compadre-assistant-stage7-20261106/admission-cabc0e2-20261106T160953Z/real-bootstrap.py')
   proof['attest_stopped']()
   ```

   Это только проверка: фактический stop выполняет родитель. Требуются
   прежний ActiveEnterTimestampMonotonic, неизменные effective ExecStart/Stop,
   новый InactiveEnterTimestampMonotonic после prepare и окно <=15 минут.
   OS state доказывает новую остановку без restart; число повторных
   идемпотентных stop команд не наблюдаемо, поэтому правило «одна команда»
   соблюдается операторским протоколом, а не заявляется доказанным systemd.
3. Повторно проверив root/bootstrap hashes, вызвать в новом isolated Python
   `proof['run_after_stop']()`. Он снова проверит stopped baseline/receipt и
   однократно создаст execution-started.json. Pipe child остаётся под guard;
   отдельный pinned migration broker выполняет ровно две миграции через
   network-none candidate image на writable private копиях. Production paths,
   live bind, tokens и polling отсутствуют. Повторный запуск case закрыт
   existing execution receipt. Не вызывать prepare повторно на real01.
4. После успешного7.3 сохранить request/result/SQLite/selector/state evidence;
   передать то же окно шагам7.4–7.5. Не запускать production между сценариями.
   Подготовку units/controller production оставить до7.7.

## Восстановить прежний рабочий сервис при отказе

Отказ prepare до stop оставляет оба канала работающими. После stop любой
неполный stop, отказ attestation/broker/case или невозможность продолжать
немедленно передаётся в7.6; не оставлять каналы выключенными до следующего чата.
Bootstrap не пытается скрыто исправлять production и не делает auto-start.

В7.6 проверить прежние checkout/state/pin/marker/IDs/image, отсутствие
проверочного migration container, затем выполнить отдельно согласованную
read-only integrity/FK/schema проверку **live** SQLite. Рабочую БД из fixture
не восстанавливать: испытание к ней не обращалось. Не удалять state/marker,
не выполнять docker prune или глобальный rm. При orphan broker разбирать
только его точное уникальное имя по broker журналу.

После подтверждения baseline выполнить один
`sudo systemctl start obs-chat-bot.service`. Проверить оба прежних exact
image/OCI/IDs, healthy/restarts/policy, отсутствие дублей, service/timer и polling;
зафиксировать UTC конца и общий простой. Если Compose пересоздал ID либо health
не восстановлен, это отклонение и recovery по OPERATIONS, а не успешный возврат.
Содержимое ответов и реальный пользовательский polling новый harness не доказывает.

## Разобрать первое окно

Родитель проверил real01 hashes/effective ExecStop и дал go на один stop.
Непосредственный pre-stop baseline 16:24:48.455 UTC был успешен. Ровно один
`systemctl stop obs-chat-bot.service` начат **16:24:48.651857 UTC** и завершён
**16:25:00.991970 UTC**. Затем `attest_stopped` отказал с
`BOOTSTRAP_STOP_TRANSITION`, не создав stopped.json. Execution receipt,
scenarios, real case и migration containers **не создавались**.

В той же административной сессии немедленно выполнено recovery7.6:
проверены checkout/state/IDs/image; live SQLite через mode=ro:
integrityok, FK0, schema1–4. Выполнен start прежнего service; к
**16:25:20.886614 UTC** оба прежних IDs healthy/running/restarts0, старый
image, polling signals Telegram/VK подтверждены. Service/timer active,
backup service inactive, state/marker baseline прежний. Интервал от stop
attempt до verified recovery **32.235 с**; точный пользовательский downtime
не измерялся. Машинные metadata — [STAGE7_3_REAL_RESULT.jsonl](STAGE7_3_REAL_RESULT.jsonl).
Родитель независимо подтвердил восстановление обоих exact IDs/image/health.

Root cause: systemctl show ExecStart/ExecStop содержит не только команду, но
и start_time/stop_time/pid/code/status. Их полный text ошибочно входил в
hash effective commands. Он закономерно изменился с `7199aee17...` на
`30d6b61bf...` после stop/start при прежних командах compose. Исходный
stopped baseline не сохранён из-за отказа; нельзя доказать, какое ещё
условие могло отказать. Известное неверное сравнение dynamic hash устранено.

Новый private bootstrap `deacc81ee38701bc0f9766a9a561b8de305e010fc7fe91baf4126e1cd11b863a`
хеширует canonical path/argv/ignore_errors; exact argv остаются закреплены.
Fail-closed parser требует один descriptor и только известные runtime fields;
unknown syntax, изменённый argv/path/ignore_errors и дополнительная команда
отвергаются. Timestamp activation/stop/freshness проверки сохранены.
CASE теперь literal real02, real01 не перезаписывается/не переиспользуется.
Новый bootstrap ещё не установлен поверх исторического real01 bootstrap.

В ad-hoc pre-stop DB-holder scan также найден overescaped regex. Его поле
manual_writers=false/holder0 в machine metadata не считается самостоятельным
доказательством отсутствия manual writers. Исправленный scan использует
явные suffix checks, распознаёт .db/WAL/SHM/journal, требует cgroup одного из
двух прежних containers и не скрывает permission/read failures. Он включён
в baseline нового bootstrap; остаётся обязательным операторское окно без
ручных writers, поскольку моментальный /proc scan не исключает процесс между
осмотрами. После recovery исправленный scan: holder0/unexpected0.

Локально project venv: **8 bootstrap tests OK, 0.026 с**. Новые tests доказывают
стабильность hash при реальных форматах dynamic systemctl descriptors,
отказ изменения команды/unknown fields/extra command, распознавание paths,
отказ unknown cgroup/PermissionError. Native Linux read-only baseline нового
кода **16:28:26 UTC** success, stable units SHA256
`15b1aa84d6ab2ae19f29a9db997b6a46e54619ec6c2ceab8c9cc8485092d6847`,
manual DB holders0, serviceactive. Никакого нового staging, stop или realcase
после recovery не выполнялось.

Следующее условие: review исправления родителем, новый prepare real02 с
новыми prepared/config/bootstrap hashes, затем отдельное go на новое окно.
Предыдущее go исчерпано единственным stop; автоматически повторять его нельзя.

## Отложить real02 до восстановления live API

Попытка нового reviewed prepare real02 остановилась **до создания staging**:
native live GitHub API ответил403, X-RateLimit-Remaining=0, reset
**06.11.2026 16:52:07 UTC**. Это ошибка внешнего admission, не разрешение
использовать старый receipt как fresh. Новый код deacc81... сохранён отдельно
в `admission-cabc0e2-20261106T160953Z/real02-bootstrap.py`; historical real01
и его bootstrap не перезаписывались. Root real02/prepared/config/deadline ещё
не созданы. Production serviceactive, stop/realcase не выполнялись.
Metadata override не расширялся, token на VPS не передавался. Следующий
действующий путь — повторить тот же prepare после API reset, затем представить
новые prepared/config hashes и deadline родителю до отдельного go.

Перед новым prepare дополнительно проверен deleted FD: bootstrap обновлён
до SHA256 `59198a1f86a5f7d888c19a8f5a36c29074835f18e875ab988603fab5f699fde3`.
Любой файл под live data вне двух исходных containers закрывает допуск,
включая ` (deleted)`, .db/.sqlite/.sqlite3, WAL/SHM/journal, attached БД без
расширения и временные имена. Проверка больше не выводит отсутствие writer
из незнакомого расширения. Temp/anon descriptors вне live data не принимаются
за live DB; краткоживущие writers всё ещё исключаются операторским окном.
Expanded subtests и unknown-cgroup deleted FD: 8 testsOK0.028с. Новый код
атомарно заменил только private real02-bootstrap.py до создания staging;
real01 сохранён. Native read-only baseline16:37:11UTC successful, active,
manual DB holders0. API admission transport и pinned receipts не менялись.

## Проверить подготовленный real02

После anonymous API reset выполнен ровно один prepare. Он успешен
**06.11.2026 16:52:51.130541 UTC**; freshness deadline
**17:07:51.130541 UTC**. До отдельного review/go второго stop нет.
Fresh native live CI/evidence, snapshot, оба image/OCI, source и baseline
прошли; artifact transport не расширялся, tokens не передавались.

| Объект | SHA256 / значение |
| --- | --- |
| Root | `/var/lib/compadre-assistant-stage7-20261106/controller-proof-cabc0e2-real02` |
| prepared.json | `f507efb5d396a63715fd37c9bba5b59ed911c8980ace30dd6d42b09018f7166a` |
| reviewed-config.json | `d9a63a8ff93fb1c272875d9fe6a2b532f9b8d3fc08c2884ff342992a0898fb7c` |
| admission/real02-bootstrap.py | `59198a1f86a5f7d888c19a8f5a36c29074835f18e875ab988603fab5f699fde3` |
| Harness | `7a4cef267e3132b8e5e786ad2803649e54f6de55a8ff47fd426673ff2286132a` |
| Stable effective commands | `15b1aa84d6ab2ae19f29a9db997b6a46e54619ec6c2ceab8c9cc8485092d6847` |

Root не содержит stopped/execution/scenarios/result. Manual DB holders0.
Post-baseline **16:52:52 UTC**: service/timeractive, backupinactive, прежние
IDs/imagehealthy/restarts0, state/sourcehash прежние. Real01 сохранён.
Машинные metadata: [STAGE7_3_REAL02_PREPARE.jsonl](STAGE7_3_REAL02_PREPARE.jsonl).

Протокол stop/attest/run/recovery выше остаётся, но для real02 функция
runpy загружает **admission-cabc0e2-20261106T160953Z/real02-bootstrap.py**,
а все case receipts относятся к **controller-proof-cabc0e2-real02**.
Исторические real01 bootstrap/hashes не использовать для нового окна.
Перед stop повторно сверить exact hashes таблицы, свежий baseline и deadline;
при отказе/просрочке не останавливать. Текущий этап — только review.

## Разобрать второе окно real02

После независимого review родитель дал отдельное go. Final baseline/hashes/
manual-holder scan успешны16:54:22.299968UTC. Один stop начат
**16:54:22.496546 UTC**, завершён**16:54:33.476972 UTC**. Stable effective
commands hash сохранился; stop attestation успешно создала stopped.json.

Child завершился до первого migration RPC: parent получил HARNESS_CHILD_PIPE;
private log указывает **ValueError HARNESS_SOURCE** в validate из первого
require_switch_rollout. Requests received/completed=0/0, private marker/result
отсутствуют. Execution-started.json создан, но realcase не завершён,
migration Docker не запускался.

Сразу в той же сессии выполнен recovery7.6: live SQLite readonly integrityok,
FK0, schema1–4; прежние checkout/state/IDs/image подтверждены; один start.
Verified recovery **16:54:53.493398 UTC**: serviceactive, исходные exactIDs,
image, healthy/restarts0 и polling Telegram/VK. Интервал от stop attempt до
verified recovery **30.997 с**. Дополнительный readonly baseline16:55:52UTC
подтвердил active service/timer, backupinactive, прежние hashes и manualholders0.
Машинный результат: [STAGE7_3_REAL02_RESULT.jsonl](STAGE7_3_REAL02_RESULT.jsonl).

Root cause подтверждён без изменений: initial harness setup вызывал
`prep.backup_sqlite(fixtures/source.db, checkout/data/app.db)`. Controller helper
открывает source с mode=ro. Для WAL-mode main SQLite такое чтение создаёт
WAL0/SHM32768 рядом с source, хотя main не меняется. Следующий strict harness
validate запрещает любые sidecars исходной fixture и правильно отказывает.
Case fixture main сохранила SHA5a7a6a..., WAL sha e3b0c442.../0bytes,
SHM sha fd4c9fda.../32768bytes. Original fixture7.1 не менялась.

Предлагаемое исправление только проверочного setup: verified closed main
копировать byte-for-byte в новый private checkout без SQLite открытия fixture;
реальные controller preflight/final backup и брокер оставить. Нужен regression
с настоящим WAL-mode source без sidecars и сохранением этого инварианта после
setup/повтора. Guard и запрет source sidecars ослаблять нельзя. Исправление и
новый reviewed staging/stop пока не выполнялись; требуется отдельное решение
родителя. Planned единое окно7.3–7.5 ещё не достигнуто: обе попытки завершены
recovery до настоящих migration RPC. Третьего stop нет.

## Подготовить real03 в открытом maintenance окне

Пользователь явно изменил порядок: выключить ботов сейчас и оставить
выключенными до завершения испытаний/7.6. Один stop начат
**16:58:07.942252 UTC**, подтверждён **16:58:19.824951 UTC**. Serviceinactive,
оба exactpreviousIDs exited, timeractive/backupinactive, дублей нет,
state/source/checkout/image прежние. Receipt
`maintenance-stop-20261106T165819Z.json` SHA256
`35f7e53afe2f4872838c8d5ad9ff687979dc29b03642733361325cd973ed4de3`.
Доказательство: [STAGE7_3_MAINTENANCE_STOP.jsonl](STAGE7_3_MAINTENANCE_STOP.jsonl).
Этот stop открыл текущее окно; новые stop/start между case не требуются.

Setup harness исправлен: после exact hash/no-sidecars validate закрытый main
копируется byte-for-byte с fsync в model checkout. Fixture SQLite не открывается.
Controller preflight/final snapshot/backup и отдельный migration broker
сохранены без изменений; guard/no-sidecars запреты не ослаблены.
WAL-mode regression: **5 local tests OK, 3.225с**. Native Linux synthetic
**16:58:57UTC success**, source header02/02, исходный hash/mtime прежние,
sidecars отсутствуют до/после; guard4, migrations2, modelstarts1, realflock,
commit/adoption success. Мигратор synthetic — callback, Docker не вызывался.
Original pinned receipts сохранены, WAL-proof записан отдельно:
[STAGE7_3_WAL_SYNTHETIC.jsonl](STAGE7_3_WAL_SYNTHETIC.jsonl).

Новый `stage7_3_maintenance_bootstrap.py` имеет только prepare и
run_in_maintenance, без stop/start/auto-recovery и без env/CLI обхода. Он
требует exact maintenance-stop receipt/epoch/units, свежий stopped baseline,
новый harness hash и reviewed native receipts. По указанию родителя API не
вызывался повторно: baseline live verifier16:52 связан с exact real02
prepared/config bytes и exact полями пары/image/evidence. Это разрешённое
повторное использование проверенной пары, а не перенос на новый candidate.
Runtime/SHA/image/snapshot/policy не менялись. **9 bootstrap tests OK, 0.049с**;
проверены wrong stop/restart/units, изменённые recent bytes/пара и unknown FD.

Real03 prepare успешен **17:02:55.088711UTC**; deadline
**17:17:55.088711UTC**. Post-baseline17:02:55UTC inactive, оба IDs exited,
timeractive/backupinactive, manualholders0, прежние state/source.

| Объект | Значение |
| --- | --- |
| Case root | `/var/lib/compadre-assistant-stage7-20261106/controller-proof-cabc0e2-real03` |
| prepared.json | `a6be6824cdd28aa9b4a1c27b767b8e20b3995762e4fab482bb2dc758ee26c8ef` |
| reviewed-config.json | `68084d7871d1803d30eed9256d88090cc67645b3514348dd87dc49f53961a174` |
| admission/maintenance-bootstrap.py | `7e9c81565d3590a7e239046f9a0c8cb18f69eae57d1897de29951b37dc5e0b15` |
| admission/harness-maintenance.py | `77b772aa94ba8cffd1de66de59360e803713e44b9572e5f5accd6141bf86da41` |

Машинный staging evidence: [STAGE7_3_REAL03_PREPARE.jsonl](STAGE7_3_REAL03_PREPARE.jsonl).
Execution/scenarios/result отсутствуют. Real broker не запускался. Старые
real01/real02 и их receipts не перезаписывались.

После review/go родителя повторно сверить hashes/baseline/freshness и
вызвать только `run_in_maintenance()` из private
`admission-cabc0e2-20261106T160953Z/maintenance-bootstrap.py`. Никаких
дополнительных stop/attest/start команд. При case failure сохранить metadata
и оставить ботов выключенными по новому указанию. Восстановление в7.6 после
завершения испытаний, либо отдельно при угрозе данным/новой команде пользователя.
