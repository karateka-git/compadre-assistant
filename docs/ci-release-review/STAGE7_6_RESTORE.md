# Шаг 7.6: проверить матрицу и восстановить рабочий сервис

06.11.2026 независимая сверка испытаний 7.3–7.5 завершена; прежний production
сервис восстановлен одним `systemctl start obs-chat-bot.service`.
Оба исходных контейнера healthy и polling, данные сохранены. Этот документ
закрывает восстановление [7.6 плана](PLAN.md#шаги-этапа-7), но не разрешает
автоматически выпуск 7.7. Правила доступа — [PRODUCTION.md](../PRODUCTION.md),
порядок выпуска и recovery — [OPERATIONS.md](../OPERATIONS.md).

## Сверить независимые доказательства

Исполнитель 7.6 не выполнял 7.4/7.5. Перед start прочитаны их отчёты,
fault/crash-injection код и bounded receipts; затем на VPS независимо
прочитаны десять private cases. Проверены закреплённые state hashes из
первичных отчётов, selector/result binding, old main, новые SQLite,
private backup и сохранившийся committed WAL. В каждом case сверены exact
candidate.tar, 63 manifest files, их SHA256/права/root owner и native receipts.
Проверка не запускала harness повторно и не меняла private state.
Результаты — [REVIEW.jsonl](STAGE7_6_REVIEW.jsonl).

| Граница | Независимо подтверждённый итог |
| --- | --- |
| [7.3 real03](STAGE7_3_RESULT.md) | success, selector=new, durable admission=true, committed тестовая запись, один model start. Два настоящих exact image CLI/RPC; private source и final backup целы. |
| [7.4 preflight](STAGE7_4_FAILURES.md) | SAFE_COPY_FINGERPRINT, failed_preflight, legacy путь без selector, starts=0; повтор не создаёт writer. |
| 7.4 backup | SAFE_FINAL_PREPARATION, исходный writers_stopped/old; recovery rolled_back/old/previous image, marker снят, starts=1. Final snapshot ещё не создан. |
| 7.4 migration | Потеря результата после успешного реального CLI, SAFE_FINAL_MIGRATION; old и final snapshot сохранены, recovery rolled_back/old/previous. Это не падение посреди нового SQL. |
| 7.4 selector | Реальный atomic replace уже выбрал new, durable switch_intent; до admission recovery вернул old/previous, marker снят. |
| 7.4 prepare | runtime_prepared до admission_recorded, candidate не запускался; recovery old/previous, rolled_back. |
| [7.5 admission](STAGE7_5_POST_ADMISSION.md) | exit=77 после durable true и до model start; два новых процесса recovery/retry сохранили new, rolled_back на exact previous, starts=1. |
| 7.5 committed WAL | exit=77 после commit с открытым connection; new inode 572250 и WAL 8272 bytes сохранены. Обычный read-only count тестового marker=1, immutable main-only count=0; SHA WAL `e965325417ff77d0bf1a126158d804ef7309c34260ccf97a6b90d4154430ff46`. Starts=2 означают последовательные candidate/recovery пары, повтор новой пары не добавляет. |
| 7.5 bad_evidence / bad_state | Обе конкретные причины отказа сверены с кодом. degraded, private marker сохранён, selected_image=null, starts=0, ни одного running model container; физический selector/new сохранён для разбора. Повреждённое admission не признаётся допустимым. |

Во всех old SQLite integrity=ok/FK=0, журнал 0001–0004 и исходный SHA256
`5a7a6aaadf16c8f1249cd8e941b599461d8c6c10c68fbb517c603a3804497e6c`.
Доступные final backup проверены по state digest и integrity/FK/schema;
snapshot digest `0fdf4baf17a3cc1f0059af9c02484eec32ea5487fdcc72c4e5464824fea2ac79`
не выдаётся за исходные main bytes. У preflight/backup отсутствие final
snapshot соответствует месту отказа. Отрицательные private markers сохранены,
их нельзя путать с production marker: в production marker отсутствует.

## Проверить точную пару и CI artifact

| Предмет | Значение |
| --- | --- |
| Candidate | `cabc0e24d28947123868db8a60f6b55095feb63e` |
| Previous, восстановлен | `dd71c2ba42a0744b5b654cedcf73009ce8879058`, v3.1.0 |
| Candidate image | `sha256:2025e11b35b9e8898ca41d740d4a10e318e048e91d0eeda04b1000c4db5bc093` |
| Previous image | `sha256:334eeaef6bb4d067ca7fe92dc092ae34362f7f559b8e0f7409351b078402c1b8` |
| Controller archive испытаний | exact candidate, 63 files; SHA256 `578f10a40946ae5f55e0ebb1c3f6458a8b7e20e18b8bccbb09bb347bcdbb9aca` |
| Full CI | [42249037867/1](https://github.com/karateka-git/compadre-assistant/actions/runs/42249037867), workflow_dispatch; controller, repository-compatibility, full-suite и metrics success. Push run с skipped full-suite не использован. |
| Artifact | 13519278785, `repository-compatibility-42249037867-1`, not expired; digest `sha256:87a5ab53bcd8b7f57a2012c279872e9252c74ebfc33ddc0f3fbd0099b2e77c6c` |
| Policy | `eb1dad4fee8a176ec4870db12da5e1051a3d1c6e` |
| Schema fingerprint | CI/runtime `11e5bb7311a50c33e8cf5f7968c8b72ad54dfae0136dc02bbccba15367c3fd15` |

Native provenance и exact-pair admission из [7.3](STAGE7_3_NATIVE_PROOF.md)
сверены по закреплённым receipt hashes. В 7.4/7.5 verifier работал offline
с проверенными bytes: нового GitHub API/revocation check после 16:52 не было.
В 7.6 дополнительно выполнен свежий GitHub API metadata recheck exact run,
всех jobs и artifact; результаты — [CI.json](STAGE7_6_CI.json). ZIP bytes
и repository worker заново не исполнялись. Это не Release verification и
не fresh admission production-команды: перед 7.7 требуется штатная проверка.

## Разделить причины ранних отказов

Два предыдущих коротких окна не входят в восстановленное maintenance окно.
Их полный разбор сохранён в [STOP_REVIEW](STAGE7_3_STOP_REVIEW.md).

- Real01: BOOTSTRAP_STOP_TRANSITION до case/RPC; hash ошибочно включал
  динамические timestamps/PID/status systemctl descriptors. Исправлен
  canonical hash exact команд. Восстановление тогда заняло 32.235 с.
- Real02: HARNESS_CHILD_PIPE с первичной HARNESS_SOURCE до первого RPC.
  `backup_sqlite` при setup открыл WAL-mode source read-only и создал
  sidecars; strict source guard корректно отказал. Setup real03 заменён
  verified byte copy, реальные preflight/final backup не подменены.
  Предыдущее восстановление заняло 30.997 с.

До этих двух окон отдельно выявлялся CI/runtime fingerprint mismatch у
исторического candidate 595d143; он исправлен до текущего cabc0e2 с новым
полным CI. 403/rate limit при подготовке также остановил admission до stop.
Эти отказы не засчитываются как успешные испытания и не скрываются real03.

## Подтвердить production перед start

Свежий read-only осмотр начат 17:36:01 UTC; проверка live SQLite и сохранности
закончена 17:40:54 UTC. Сверено то же maintenance окно: monotonic activation
1565602704434, inactive_enter 1565824916781 и stop receipt SHA256
`35f7e53afe2f4872838c8d5ad9ff687979dc29b03642733361325cd973ed4de3`.

Checkout чистый previous SHA; ровно два прежних exited IDs, restarts=0,
unless-stopped. Дополнительных containers, включая private migration, нет.
Оба image/OCI exact; runtime.env соответствует прежнему pin, оба Compose
overrides совпадают с Git previous. Service inactive/enabled, timer
active/enabled, backup service inactive/success. NeedDaemonReload=no;
effective ExecStartEx/ExecStopEx и flags=no-setuid прежние. Condition marker
подтверждён структурированным D-Bus чтением: `systemctl show` печатает
Conditions как `[unprintable]` и сам по себе его не доказывает.

Все 38 ordinary files установленного controller совпали с Git previous;
inventory digest `dc683481109242bc88eae2b3350c86829930e716b6a6b6b5783420b3f8d795bb`.
Controller/wrapper/units и production state не заменялись. Производственный
deploy.lock удержан при start без удаления/замены inode 279276.
Мгновенный /proc FD scan не обнаружил live data holders до start; это не
заменяет операторское окно без ручных writers.

Live SQLite проверена `mode=ro&immutable=1` после подтверждения WAL=0:
integrity=ok, FK=0, schema 0001–0004. Данные всех таблиц сравнены без вывода
строк: для каждого row вычислен SHA256 от `repr(tuple(row))`, хеши отсортированы
с сохранением повторов, затем вычислен общий digest с именами таблиц.
Live и закрытый source имеют одинаковый logical SHA256
`78bf4bb4f1f49ec29a436568c272007c4df52f5030508d3bc54e1b31a1b88105`.
Физический live main SHA256 отличается от source; сохранность не выводится
из равенства main-файлов. Хеш live main до и после восстановления совпал.

## Восстановить один раз и проверить независимо

Применено действующее указание пользователя: боты оставались OFF между
испытаниями до 7.6. После успешного review выполнен один start, без deploy,
миграции, изменения controller, DB, marker/state или restart policy.
Полные logs/inspect и пользовательские строки не публиковались.

| Метрика | UTC / длительность |
| --- | --- |
| Stop attempt maintenance окна | 06.11.2026 16:58:07.942252 |
| Оба stopped подтверждены | 16:58:19.824951 |
| Единственный start attempt 7.6 | 17:42:17.430487 |
| systemctl start завершился rc=0 | 17:42:18.627036 |
| Оба healthy/polling независимо подтверждены | 17:42:34.822177 |
| Stop attempt → verified recovery | **2666.879925 с = 44 мин 26.880 с** |
| Confirmed stopped → verified recovery | 2654.997226 с |
| Start attempt → verified recovery | 17.391690 с |

Это измеренные административные границы, а не точный интервал отсутствия
ответов пользователю. Два прежних окна 32.235/30.997 с приведены отдельно.

| Канал | Прежний точный container ID |
| --- | --- |
| Telegram | `2798069b3f96d3a31c9d3a9d7b12e52c3a754fa4d5348798963209a38869fe42` |
| VK | `31a165fee7911fc6f51690956954c4d6258e433c1f8a9c768295a3cdf6a15a00` |

Оба работают на previous image ID из таблицы выше, healthy, restarts=0,
unless-stopped; контейнеры не пересозданы. Ограниченное чтение logs только
с нового start, tail=500, подтвердило polling signal обоих каналов; traceback
и Telegram polling conflict в этом окне не найдены. Это наблюдение сервера,
не пользовательская проверка живых ответов.

Сервис и timer active/enabled; backup service inactive. Следующий backup
timer — 07.11 03:15 UTC, последний trigger 06.11 03:15:33 UTC. Timer вручную
не менялся. Последний отдельный read-only postcheck в **17:45:35 UTC**
подтвердил те же healthy IDs/image, clean previous checkout, отсутствие
дублей/лишних containers/manual holders и целостность live SQLite. При этом
соединения административного reader явно закрыты до /proc scan, чтобы не
считать собственное read-only соединение посторонним writer.

| Неизменный объект | SHA256 до и после start |
| --- | --- |
| Live main | `6be099a06ce731010c624dceb2215580bec263229c124afa95e1982677ab098c` |
| Все табличные данные live/source | `78bf4bb4f1f49ec29a436568c272007c4df52f5030508d3bc54e1b31a1b88105` |
| Production state.json | `fc0036ce799f6a850b4c0a6702aeda711870aea15456e7ede03b7c284f6ad317` |
| runtime.env | `0562237e1ad3cff3f971225d07f82a97e66fdeb481f2aca4ca6e3d1616c2fa9a` |
| pinned-compose.yml | `26c895bc5ec62e29861afd26675ebe5f7a22d2445cea5227b9eadd2bf5ec2444` |
| critical-compose.yml | `7e2eb2c7944afe7fd64b6b21605b86a029e189e0b099969800f4a9f7fe06b862` |

Original closed source/main/WAL/SHM hashes сохранены. Production marker,
selector, controller.sha и recovery-blocked отсутствуют. Исторический
release backup v3.1.0 дополнительно проверен: mode0600, integrity=ok/FK=0,
schema0001–0004. Его данные отличаются от source/live, потому что это
отдельная историческая копия, не текущий backup; равенство не требуется
и восстановление из него не выполнялось. Backup точного окна 7.7 ещё нужен.

Bounded результаты start и final proof — [RESTORE.jsonl](STAGE7_6_RESTORE.jsonl).
Исходный код не менялся, тесты приложения не запускались: выполнены
необходимые административные проверки реального контура. Commit/push/merge
и шаг 7.7 не выполнялись.

## Передать остаточный риск перед 7.7

7.6 завершён для review родителем: матрица на копиях сверена, рабочий сервис
восстановлен. Прежний production real systemd/start/health/polling проверен;
те же операции **нового** candidate в настоящем выпуске ещё не проверены.
На копиях использованы реальный CLI exact image и SQLite/FS/flock, но
Docker create/start, systemd и polling controller оставались моделью.
В данной exact паре новых SQL/DDL нет: повторные migration CLI не доказывают
падение посреди новой SQL-миграции или readonly admission mounts новой схемы.

Reboot, power loss, kernel crash и fsync при потере питания не инъецировались;
process exit=77 их не заменяет. Испытания и aggregate scan не блокируют
доверенного администратора от будущей ручной записи. В 7.7 заново нужны
актуальный read-only VPS baseline, release/CI/compatibility admission точного
SHA, отдельная проверенная установка controller snapshot и нужных units,
backup текущей рабочей БД и согласованное окно без ручных writers.
Живые ответы обоих каналов остаются отдельной пользовательской приёмкой.
При изменении candidate SHA или installed previous/image старые доказательства
не переносятся. Этот отчёт не начинает production deploy.
