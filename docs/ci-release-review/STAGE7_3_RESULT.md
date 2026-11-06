# Результат 7.3: штатный controller на закрытой копии

06.11.2026 real03 успешно прошёл штатный путь на закрытой копии production
через exact candidate image и отдельный migration broker. Родитель независимо
сверил result/state/selector hashes и serviceinactive/timeractive; шаг7.3
завершён. Шаг7 целиком не завершён; боты остаются выключены
для7.4–7.5 в уже открытом пользователем maintenance окне. Код/допуск и история
двух прежних отказов — [STAGE7_3_STOP_REVIEW.md](STAGE7_3_STOP_REVIEW.md).

## Проверить точную пару и результат

Candidate `cabc0e24d28947123868db8a60f6b55095feb63e`, previous
`dd71c2ba42a0744b5b654cedcf73009ce8879058`. Full CI42249037867/1 success;
artifact13519278785/digest87a5ab53..., policyeb1dad4..., exact snapshot63files
SHA578f10a4..., image
`sha256:2025e11b35b9e8898ca41d740d4a10e318e048e91d0eeda04b1000c4db5bc093`,
OCI exact candidate. Полные admission receipts —
[STAGE7_3_NATIVE_PROOF.md](STAGE7_3_NATIVE_PROOF.md).

Reviewed root:
`/var/lib/compadre-assistant-stage7-20261106/controller-proof-cabc0e2-real03`.
После fresh hashes/maintenance baseline **17:04:17 UTC** ровно один
`run_in_maintenance()` завершился success **17:04:37.088220 UTC**:

- Guard отверг subprocess, socket, production read и external write; исключения
  для broker в child не добавлялись. Настоящий Linux flock сохранён.
- Два ограниченных request/result RPC вызвали реальный exact image
  `--migrate` на отдельных writable копиях, networknone/read-onlyrootfs,
  единственный private DB bind, без tokens, live bind, Telegram/VK и polling.
- Выполнены реальные preflight/final SQLite snapshot/backup и проверки,
  selector переключён на new, durable admission сохранён, test row committed.
- Повторный model boot/read-only adoption не создал второго writer:
  modelstarts1, real_pollingfalse. Сохранность original fixture доказана
  main SHA/mtime и отсутствием новых sidecars; original7.1 main/WAL0/SHM не менялись.

**migrations=2 означает два RPC/CLI прогона, а не две новые SQL миграции.**
Закрытая копия и candidate уже имеют одинаковый журнал0001–0004; новые SQL
в этой паре отсутствуют. WAL synthetic regression отдельно прошёл настоящий
controller path с callback от старой схемы к текущей; это не Docker/production
polling доказательство и не подмена real image CLI.

Дополнительная проверка **17:05:49 UTC** на выбранной private SQLite:
integrityok, FK0, schema1–4, CI/runtime fingerprint
`11e5bb7311a50c33e8cf5f7968c8b72ad54dfae0136dc02bbccba15367c3fd15`,
committed marker count1. Selector generation==new generation==durable selected;
state stageSUCCESS, admission_may_have_startedtrue, boot exactSHA/image,
RPCreceived2/completed2. User rows и broker process output не публиковались.

## Сохранить evidence

Private `result.json`, `transport.jsonl`, `independent-proof.json`,
`execution-started.json` и весь `scenarios/happy` сохранены. Transition:
`530ed0ac1c4f4fa5a0831a4f65461d80`; selected generation:
`dd24cd0f476346d7b303c4a089965a5f`.
Receipt binding `ef3990578aa66c5e64f1465cb1462fd0cbb5c0bc79089f33f4b7ba5819b1bc0f`.

| Файл | SHA256 |
| --- | --- |
| result.json | `7a779a86a94e519b3d847485be1478de59bca69664354d9532c7d2323306ba31` |
| transport.jsonl | `6c90a2f01d4f599266bcc08eb1feabad0f3539ded4a2970656efe7a59bc63bb0` |
| scenarios/happy/state/state.json | `dff8105f66024b4b93c5df15210401949db30a66624a0159bb47f7cf4317b1c3` |
| scenarios/happy/state/active-db.json | `8458965f5235f9bbae0c1801f64231f11f91913c7740fdc9253e30f9f53169b8` |
| execution-started.json | `34d89eda0a2578f880a47abb3c1efb362bd6c1b7c63fced78af658488ee7ddeb` |

Локальные metadata:
[REAL03_RESULT](STAGE7_3_REAL03_RESULT.jsonl),
[REAL03_PROOF](STAGE7_3_REAL03_PROOF.jsonl),
[REAL03_PREPARE](STAGE7_3_REAL03_PREPARE.jsonl),
[WAL_SYNTHETIC](STAGE7_3_WAL_SYNTHETIC.jsonl).
Runtime/controller source и candidate SHA не менялись. Только проверочный setup
переведён на byte copy; 5 harness tests и 9 maintenance bootstrap tests успешны.
Новые native receipts не подменяют historical snapshots; API повторно после
16:52 admission не вызывался по review родителя.

## Передать то же maintenance окно

Начало текущего stop attempt **16:58:07.942252 UTC**, оба бота подтверждённо
выключены **16:58:19.824951 UTC**. После case/latest proof serviceinactive,
оба exact previousIDs exited/restarts0, timeractive/backupinactive,
manualholders0, source/state/checkout/image/units baseline прежние.
Production SQLite в этом case не открывалась; production state/controller/units
не менялись. Два migration containers удалены broker cleanup; других containers
кроме прежней остановленной пары нет.

По явному указанию пользователя автоматическое восстановление после отказов
не выполняется. После review7.3 передать то же окно7.4–7.5 без stop/start.
Рабочий сервис восстановить и проверить в7.6 после испытаний; досрочно — лишь
при угрозе данным/отдельной команде. Real Docker/systemd/start/health/polling
нового выпуска остаются в7.7; здесь проверялась закрытая копия с model boot.
