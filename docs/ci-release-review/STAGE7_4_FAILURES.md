# Шаг 7.4: проверить отказы до admission на Linux-копиях

06.11.2026 пять отказных сценариев завершились ожидаемым восстановлением
на отдельных закрытых копиях. Этот отчёт передаётся на review родителя
перед [7.5](PLAN.md#шаги-этапа-7). Допуск точной пары и успешный путь —
[результат 7.3](STAGE7_3_RESULT.md); maintenance-окно продолжает действовать,
**production-боты остаются выключенными до 7.6 по указанию пользователя**.

## Сохранить допуск и исходные данные

Runtime/controller source не менялся. Использованы те же candidate
`cabc0e24d28947123868db8a60f6b55095feb63e`, previous
`dd71c2ba42a0744b5b654cedcf73009ce8879058`, полный CI `42249037867/1`,
63-file snapshot с tar SHA256
`578f10a40946ae5f55e0ebb1c3f6458a8b7e20e18b8bccbb09bb347bcdbb9aca`
и native receipts из [7.3](STAGE7_3_NATIVE_PROOF.md).

Мигратор использовал только candidate image
`sha256:2025e11b35b9e8898ca41d740d4a10e318e048e91d0eeda04b1000c4db5bc093`.
Recovery модели выбрал previous image
`sha256:334eeaef6bb4d067ca7fe92dc092ae34362f7f559b8e0f7409351b078402c1b8`.
Baseline каждого case повторно сверял оба image/OCI. Native receipt hashes
и live admission 16:52 из real02 проверены по прежнему контракту 7.3;
новых запросов GitHub API не было.

Fresh read-only baseline 17:16:01 UTC подтвердил прежний stopped контур.
Подготовка завершилась 17:16:27 UTC. Каждый case получил новый root:

```text
/var/lib/compadre-assistant-stage7-20261106/controller-proof-cabc0e2-fault-<name>-01
```

Здесь `<name>` — `preflight`, `backup`, `migration`, `selector`, `prepare`.
Это root:root 0700 каталоги с отдельными tar/control, receipt, fixture,
checkout/data, state, selector, lock и transport log. Writable real03 не
переиспользовался. Private bootstrap/harness сохранены в
`/var/lib/compadre-assistant-stage7-20261106/fault-probe-20261106T1720Z`;
суффикс — идентификатор, не фактическое время создания.

Закрытый исходник 7.1 main копировался побайтно; пустой original WAL и
original SHM не переносились и не менялись. Каждая fixture и начальная
`checkout/data/app.db` имела SHA256
`5a7a6aaadf16c8f1249cd8e941b599461d8c6c10c68fbb517c603a3804497e6c`.
Начальный private state создавался harness из exact receipt: old SHA/image/tag,
candidate SHA/image/CI, schema_before/after, compatibility metadata,
timer_before; это отдельное состояние модели, не запись production state.

## Проверить границу и повтор

[Harness 7.4](stage7_4_harness.py) основан на transport 7.3; guard устанавливается
до imports controller. Каждый запуск проверяет запрет subprocess, socket,
production read и external write. Единственная внешняя операция — прежний
проверенный migration RPC к отдельному broker из snapshot, с exact image,
private mount, network=none и без каналов/credentials. Все операции
Docker create/start/inspect и systemd в controller — закрытая модель.

Каждый отказ завершается сохранением private state и model inventory;
recovery и ещё один повтор работают в **двух новых отдельных процессах**.
Это инъекции исключений на конкретных границах, не SIGKILL/reboot/power loss.
Код проверяет ожидаемые stage и reason: случайный другой отказ не считается
успехом. После отказа нигде не записано durable admission=true и model starts=0.

| Отказ | Инъекция и состояние после неё | Первый повтор | Второй повтор / завершение UTC |
| --- | --- | --- | --- |
| Preflight | После реальной миграции предварительной копии verifier получает неверный fingerprint: `SAFE_COPY_FINGERPRINT`, `failed_preflight`, selector отсутствует, marker=false | Настоящий `_recover` сохраняет legacy old/image, starts=0 | Те же state/hash, 17:16:54 |
| Backup | OSError перед созданием final backup после register_initial: `SAFE_FINAL_PREPARATION`, `writers_stopped`, selected=old, marker=true | `rolled_back`, old/previous image, marker=false, starts=1 | Без нового start/create и изменения state/selector, 17:17:07 |
| Migration | Реальный final CLI успешно завершён, после ответа RPC инъецирована потеря результата: `SAFE_FINAL_MIGRATION`, `snapshot_verified`, selected=old, marker=true | `rolled_back`, old/previous image, marker=false, starts=1 | Без новых writers, 17:17:27 |
| Selector | Настоящий atomic replace уже выбрал new; OSError до записи `database_selected`: durable `switch_intent`, selected=new, marker=true | Настоящий recovery вернул old и previous image, `rolled_back`, marker=false, starts=1 | Те же state/selector, 17:17:47 |
| Подготовка запуска | Создана и проверена model-пара candidate, durable `runtime_prepared`; OSError до `admission_recorded`, selected=new, marker=true | Candidate не запускался; old/previous image, `rolled_back`, marker=false, starts=1 | Те же state/selector, 17:18:09 |

Исполнялись настоящие `prod._recover`, `switch.recover`, atomic state/selector
writers, runtime pin, identity/schema/integrity checks и `runtime.finalize`.
Внешний Git restore моделируется проверкой exact old SHA. Для old/old
`prepare_runtime_schema` проверяет unchanged old manifest и записывает
`runtime_schema=None`: Git checkout и schema admission для old/new здесь
не испытывались. До admission recovery не использует новый CI-вызов для
old/new: выбрана исходная old БД.

Preflight не достиг регистрации selector, поэтому его корректный итог —
legacy путь без selector, а не искусственно созданный v2. В этой модели
изначальная пара остановлена, как maintenance baseline; результат preflight
не объявляется доказательством непрерывного живого polling.

## Сверить данные, marker и lock независимо

Read-only проверка 17:18:57 UTC отдельно прочитала private result/state/model,
сверила их хеши, selector/boot generation, точный previous SHA/image и
schema/integrity/FK. Все пять old main сохранились **побайтно** с исходным
SHA256; integrity=ok, FK=0, журнал 0001–0004. User rows не выводились.

Для migration/selector/prepare сохранён настоящий final snapshot:
SHA256 `0fdf4baf17a3cc1f0059af9c02484eec32ea5487fdcc72c4e5464824fea2ac79`,
integrity=ok, FK=0, журнал 0001–0004. Отличие от main исходника связано с
нормализацией standalone snapshot; копия не подставлялась вместо old.
Для preflight/backup final snapshot не был создан — это ожидаемая граница.

| Case | Один и тот же Linux lock inode во всех процессах | Конечный SHA256 state |
| --- | --- | --- |
| preflight | 571722 | `63f431b403dc5811d74b6b2d1141e54db597ec29ac59513f5fb6859ec36a537f` |
| backup | 571748 | `80220be6794b42100c89a64f407dcc3806cd98b8ed26760dd143c532fffcddae` |
| migration | 571777 | `0eb450f2feff7ddc55cada5a5beefb34b2f6f2f5399eb55f52b7377034113bff` |
| selector | 571807 | `1a22c46187717e3db0ca0a0a0ddf216c54c0d33a47fadff9da04c22c89e6f2bd` |
| prepare | 571838 | `9c45aaff2c93a39f10d21785cd16ae402e2a90e47b8ae47f7eaab29818a74222` |

Lock не заменялся/не удалялся. Все пять final marker отсутствуют. Четыре
v2 selector однозначно указывают old; boot и обе model записи содержат
previous image. Полные selector/result/transport hashes сохранены в
[RESULTS](STAGE7_4_RESULTS.jsonl), prepared hashes — в
[PREPARE](STAGE7_4_PREPARE.jsonl), независимая проверка — в
[PROOF](STAGE7_4_PROOF.jsonl).

Восемь migration RPC приняты и успешно завершены: 1+1+2+2+2. Это восемь
запусков exact image CLI, **не восемь SQL-миграций**: у пары одинаковый журнал
0001–0004. Migration fault проверяет потерю результата после успешного CLI,
а не падение посреди нового SQL. После model recovery повторные `_recover`
и `runtime.start` не добавили commands или writers; дополнительно второй
новый процесс дал идентичный результат, state и selector hashes.

## Передать рабочий контур и ограничения

Итоговый baseline 17:18:57 UTC подтвердил ту же activation и остановку:
active_enter=1565602704434, inactive_enter=1565824916781,
effective units digest `15b1aa84d6ab2ae19f29a9db997b6a46e54619ec6c2ceab8c9cc8485092d6847`.
Service inactive; timer active, backup service inactive; прежние два exact
IDs exited/restarts=0, без дополнительных контейнеров; manual DB holders=0.
Checkout previous SHA чистый, production state digest прежний, marker/selector
отсутствуют. Original source main/WAL/SHM bytes/mtime/size совпали с 7.1.
Production SQLite не открывалась. Новых stop/start, изменения controller,
production units/state/marker/lock/DB, commit или push не было.

Проверочные исходники:

- Harness SHA256: `72e11325690944e5bf508409a666de7c3899c5a9c822e6f5c9148dea31a902ac`.
- [Bootstrap](stage7_4_bootstrap.py) SHA256: `791df42dd35bb46ee0b098b0e1ed7ae9a3ebf6f1f7c011471e0cd6664f1a0cb6`.
- [Локальный тест](stage7_4_harness_test.py): один целевой test, пять subtests,
  15 отдельных child процессов, OK за 8.801 с. Использован явный Python
  `C:/Users/compadre/Documents/obsChatBot/.venv/Scripts/python.exe`; `.venv`
  worktree отсутствует. Windows lock моделируется, Linux lock проверен отдельно.
- `git diff --check` успешен; runtime source не менялся.

7.4 проверяет Linux FS/SQLite и настоящее recovery поверх model transport.
Он не доказывает живой Docker polling, production systemd, reboot или power
loss. Эти ограничения не сняты. До 7.5 требуется review родителя;
восстановление production — только по 7.6/указанию пользователя.
