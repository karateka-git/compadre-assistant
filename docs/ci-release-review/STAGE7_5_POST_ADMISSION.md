# Шаг 7.5: сохранить новое поколение после process crash

06.11.2026 два положительных process-crash сценария и два отрицательных
сценария повреждения доказательств прошли на новых Linux-копиях.
Отчёт передаётся на review перед [7.6](PLAN.md#шаги-этапа-7).
Предыдущие границы — [7.4](STAGE7_4_FAILURES.md), exact admission и образ —
[7.3](STAGE7_3_RESULT.md). **Боты остаются выключенными с maintenance stop
16:58 UTC; 7.5 не выполняет production stop/start.**

## Закрепить private контур и допуск

Candidate `cabc0e24d28947123868db8a60f6b55095feb63e`, previous
`dd71c2ba42a0744b5b654cedcf73009ce8879058` и controller source не менялись.
Использованы те же reviewed full CI `42249037867/1`, native CI/image/synthetic
receipts, artifact/policy и snapshot:
`578f10a40946ae5f55e0ebb1c3f6458a8b7e20e18b8bccbb09bb347bcdbb9aca`.
Точные receipt hashes и происхождение — в [native proof](STAGE7_3_NATIVE_PROOF.md).

Candidate image:
`sha256:2025e11b35b9e8898ca41d740d4a10e318e048e91d0eeda04b1000c4db5bc093`.
Previous image:
`sha256:334eeaef6bb4d067ca7fe92dc092ae34362f7f559b8e0f7409351b078402c1b8`.
Fresh maintenance baseline 17:28:27 UTC подтвердил оба exact image/OCI,
прежние stopped IDs, отсутствие лишних containers и manual holders.

В 17:28:45 UTC подготовлены четыре новых root:root 0700 каталога:

```text
/var/lib/compadre-assistant-stage7-20261106/controller-proof-cabc0e2-post-<case>-01
```

`<case>` — `admission`, `wal`, `bad_evidence`, `bad_state`. В каждом собственные
fixture, state, lock, selector, checkout/data, snapshot, receipt и logs.
Writable cases 7.3/7.4 не переиспользовались. Исходная main-копия имеет SHA256
`5a7a6aaadf16c8f1249cd8e941b599461d8c6c10c68fbb517c603a3804497e6c`;
original main/WAL/SHM bytes/mtime/size сохранены по baseline 7.3.

Проверочный bundle:
`/var/lib/compadre-assistant-stage7-20261106/post-probe-20261106T1730Z`.
Суффикс — идентификатор. [Harness](stage7_5_harness.py) SHA256
`b7eb07874247df2dae47180bd2bb21520438e29175aec9daec0a10a3f6f1623f`;
[bootstrap](stage7_5_bootstrap.py) SHA256
`c97542a8ceb8d336050bab4d725afd968562edd29f308192fcc191c2b87f3c3c`.

## Прервать процесс и повторить controller

В каждом case preflight и final copy проходят настоящий exact image CLI
через прежний ограниченный migration broker. После настоящей durable записи
`admission_recorded` и до start либо после `candidate_started`/WAL commit
child вызывает `os._exit(77)`. Родитель требует именно rc=77, сохранённый
state/hash и неизменную SQLite identity; закрытие connection и flock через
finally не выполняется. Это реальный выход процесса, не исключение в стеке,
но **не reboot, kernel crash или power loss**.

`crash-state.json`, `crash-model.json`, `fault.json` и `result-crash.json`
сохраняют исходную границу отдельно от последующего recovery state.
Два повтора каждого case — два новых isolated Python-процесса с теми же
private state/SQLite. Guard установлен до imports controller; subprocess,
socket, production read и external write закрыты и проверены каждым child.
Никаких исключений guard для Docker/systemd нет; real migration работает
отдельным broker, а create/start/inspect/units controller остаются моделью.

| Case | Состояние при exit=77 | Первый и второй повтор | Конец case UTC |
| --- | --- | --- | --- |
| admission | Durable true, selected=new, stage=admission_recorded; model starts=0 | `rolled_back`, new сохранена, exact previous image, marker=false; starts=1, второй повтор не меняет state/selector и не запускает новую пару | 17:29:26 |
| wal | Durable true, selected=new, stage=candidate_started; одна model-пара; SQLite WAL commit выполнен, connection оставлен открытым | `rolled_back`, та же new SQLite/WAL и тестовая запись, exact previous image; суммарно starts=2: один candidate, затем один recovery, без одновременных пар и без нового start при повторе | 17:29:47 |
| bad_evidence | Как admission, до любого model start | Перед первым retry повреждён только private `compatibility_evidence.artifact_digest`; настоящий Host отверг mismatch, `degraded`, starts=0, private marker=true; второй retry также закрыт | 17:30:06 |
| bad_state | Как admission, до любого model start | Перед первым retry повреждён private `admission.sha`; настоящий boundary validator отверг привязку, `degraded`, starts=0, private marker=true; второй retry закрыт | 17:30:25 |

Обе отрицательные проверки сверяют конкретную первичную причину отказа:
`Recovery evidence или fingerprint изменились` и
`Граница приёма не привязана к поколению и runtime`. Случайный отказ guard,
permissions или другой причины не засчитывается. В corrupted state сохраняется
физический selector new; это сохранение данных для разбора, **не признание
повреждённого admission допустимым**. При повторном отказе updated_at/state
могут измениться; неизменны данные, selector и отсутствие start.

## Проверить recovery evidence без подмены правила

`Host.require_recovery_evidence`, `prod._recover`, `switch.recover`, guards,
state/selector writers, runtime pin и finalize исполняются из exact snapshot.
Подменены только внешние Release lookup и сетевой `verify_evidence`:

1. На каждом вызове verifier сверяются exact hashes native receipts и tar/control,
   candidate/previous SHA, run/attempt, policy, artifact ID/digest, image/OCI,
   schema fingerprint и полный receipt binding.
2. Вернувшиеся metadata сравнивает **настоящий** `Host.require_recovery_evidence`
   с durable candidate/evidence/new fingerprint и private policy pin.
3. Select возвращает фиксированные model Release metadata для той же пары;
   строка в private `github-read-token` — offline placeholder, не credentials.
4. Git restore — модель exact previous SHA. Так как native old/new имеют
   одинаковый журнал 0001–0004, schema adapter проверяет этот факт и выбирает
   `runtime_schema=None`; путь readonly admission mounts для новой схемы
   этим шагом не доказывается.

Это **offline reuse проверенного admission 7.3**, согласованное родителем.
Нового GitHub API и проверки отзыва artifact после 16:52 здесь не было.
Нельзя переносить этот verifier в production или выдавать его за fresh
release admission 7.7. Точный image pin и модель runtime проверены отдельно
от совместимости repository-кода из CI.

## Подтвердить committed WAL и сохранность данных

Независимый read-only proof завершился 17:31:28 UTC. Во всех четырёх случаях:
integrity=ok, FK=0, журнал 0001–0004, настоящий runtime schema fingerprint
`11e5bb7311a50c33e8cf5f7968c8b72ad54dfae0136dc02bbccba15367c3fd15`
совпал с CI и durable new. Old main остался побайтно исходным; final snapshot
SHA256 `0fdf4baf17a3cc1f0059af9c02484eec32ea5487fdcc72c4e5464824fea2ac79`
сохранён. Пользовательские строки не выводились.

В `wal` установлен WAL mode, `synchronous=FULL`, `wal_autocheckpoint=0`.
После commit процесс вышел с открытым connection. Parent до recovery сверил
байты main/WAL с сообщением child. После обоих retry и независимого чтения:

| Предмет | Результат |
| --- | --- |
| New inode | 572250, не менялся |
| Main SHA256 | `85ad20981384438d194b41e9213a1f50c206b1e4b237a17087b54ae64ec7042e`, не менялся |
| WAL | 8272 bytes, SHA256 `e965325417ff77d0bf1a126158d804ef7309c34260ccf97a6b90d4154430ff46`, не менялся |
| Тестовая запись | Обычное read-only чтение main+WAL: count=1; immutable чтение только main: count=0 |
| Selector | SHA256 `7646dd33bf667c681079e03d42bf89c6c808db4304c65aa0765b373a68a63a30`, не менялся; new generation совпала с boot |

Это доказывает сохранность committed WAL, а не только main-файла без записи.
Recovery не подставлял old или backup и не менял поколение.

Lock inode сохранился во всех процессах каждого case:
admission=572196, wal=572231, bad_evidence=572268, bad_state=572305.
После crash Linux освободил настоящий flock, новый процесс его захватил.
Model create разрешён только при отсутствии running-пары; recovery сначала
останавливает candidate, затем создаёт previous. Суммарные два starts в WAL
означают две последовательные пары, не второго одновременного writer.

Сохранены [PREPARE](STAGE7_5_PREPARE.jsonl), [RESULTS](STAGE7_5_RESULTS.jsonl),
[PROOF](STAGE7_5_PROOF.jsonl): точные state/selector/result/transport hashes,
inode, UTC и bounded metadata. Восемь RPC/CLI мигратора завершены и удалили
только собственные containers; это не восемь новых SQL-миграций — новых SQL
в данной exact паре нет. Полные private logs и SQLite не публикуются.

## Передать критерий для review 7.6

[Локальный тест](stage7_5_harness_test.py) прошёл из явного project venv:
один test, четыре subtests, 12 child процессов, OK за 7.087 с.
Использован `C:/Users/compadre/Documents/obsChatBot/.venv/Scripts/python.exe`;
`.venv` worktree отсутствует. Только Windows synthetic test моделирует lock
и private_text permissions; на Linux эти проверки настоящие.
`git diff --check` успешен. Runtime source, commit/push/merge не менялись.

Критерий 7.5 выполнен в private контуре: после true и process crash новая
SQLite/committed WAL сохраняется; exact admitted previous runtime выбирается
только по подтверждённому evidence; повреждённые state/evidence закрывают start.
Непроверенные границы — fresh GitHub API/revocation, живой polling/systemd,
новые DDL/schema admission mounts, reboot и power loss.

Последний baseline **17:31:28 UTC**: service inactive, оба exact previous IDs
exited/restarts=0; timer active, backup inactive; manual holders=0; лишних
containers нет. Production state/checkout/units и original source/sidecars
сохранили baseline. Production marker/selector отсутствуют. Private markers
двух негативных cases намеренно сохранены вместе с degraded state.
Production SQLite не открывалась, controller/units/DB/lock не менялись,
stop/start не выполнялись. Следующий шаг — review родителя и 7.6;
самостоятельно возобновлять ботов в 7.5 нельзя.
