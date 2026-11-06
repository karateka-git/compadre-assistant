# Шаг 7.3: native admission, image и блокер fingerprint

Запись 06.11.2026 описывает проверку точного кандидата на Linux до остановки
каналов. Шаг 7.3 **не завершён**: native synthetic proof выявил расхождение
CI/runtime fingerprint; реальный case на закрытой копии и остановка запрещены
до нового полного допуска и review. Основной порядок — [PLAN.md](PLAN.md),
подготовка — [STAGE7_3_PREFLIGHT.md](STAGE7_3_PREFLIGHT.md).

## Проверенные доказательства кандидата

| Поле | Наблюдение |
| --- | --- |
| Candidate / previous SHA | `595d1432ea4c39e1356f3f35dcb9c74530fa6b17` / `dd71c2ba42a0744b5b654cedcf73009ce8879058` |
| CI | [42245216949/1](https://github.com/karateka-git/compadre-assistant/actions/runs/42245216949), completed/success |
| Trusted policy SHA | `eb1dad4fee8a176ec4870db12da5e1051a3d1c6e` |
| Artifact ID / digest | `13515859680` / `sha256:c705afa39a91ac13faec4f95fa0f1cfd9fb80e8ad773027057e4def914e1778a` |
| CI schema fingerprint | `11e5bb7311a50c33e8cf5f7968c8b72ad54dfae0136dc02bbccba15367c3fd15` |
| Snapshot | Exact SHA candidate, 63 файла; native `verify_directory(require_root=True)` success |
| Snapshot archive SHA256 | `6d55d174a0d8c325825394d300cb538abc51dc8bf1e8dd8fd3b53c4ff1d141f2` |
| Image ID / OCI revision | `sha256:63fab922a11415a5bc3f5d79c3613a07e40acb627f41a8ea00a06b251c50c8d2` / exact candidate SHA |
| Build context SHA256 | `4f53546fea86b05e4dcd8fdd76d469554249843229d7a57a1338bf165ba5c2f2` |
| Closed 7.1 source SHA256 | `5a7a6aaadf16c8f1249cd8e941b599461d8c6c10c68fbb517c603a3804497e6c` |

Native verifier success: 15:52:14 UTC; build success: 15:56:52 UTC. Private root:
`/var/lib/compadre-assistant-stage7-20261106/admission-595d143-20261106T155205Z`.
Receipts `ci-receipt.json`, `image-receipt.json`, snapshot `control/` и архивы
сохранены root:root в приватном каталоге. Release не проверялся: это main
candidate evidence, не выпуск и не разрешение production deploy.

`stage7_3_linux_admission.py` запускает настоящий snapshot verifier на Linux.
Анонимные live GitHub metadata проверяются штатными get/pages. Единственный
override — download artifact с фиксированным ID `13515859680`: только заранее
полученные ZIP bytes, digest которых native verifier сверяет с live metadata.
Произвольного proxy/API доступа нет; GitHub token на VPS не передавался.
`stage7_3_admission.py` — Windows диагностика, не admission: Windows Path
отвергает Linux absolute paths в report. Этот отказ не обходился.

`stage7_3_image_proof.py build` использовал exact Git archive Dockerfile,
requirements.txt, compadre_assistant и document_chunker. Ни .env, ни .git,
ни production bind, ни secrets в context не входят. Построен отдельный image;
health/migration/runtime контейнеры не запускались.

## Проверка границы и отказ

Native Linux synthetic case прошёл guard (четыре отказа subprocess/network/
production read/external write), настоящий private flock, две миграции через
ограниченный request/result broker, preflight/final snapshot, selector,
durable admission, запись и повтор без второго model writer. Поля child
result: migrations=2, model_starts=1, selected=new, row_committed=true,
source_unchanged=true, real_flock=true, real_polling=false. Миграции этого
synthetic case выполнялись отдельным parent callback через migration_runner
на синтетической БД; это **не** доказательство Docker migration закрытой копии.

Внешняя проверка fingerprint корректно отказала, поэтому итоговый synthetic
admission receipt не выдан. На одних и тех же sqlite_schema rows trusted worker
получает `11e5bb...`, runtime —
`3ca749997ef83f0bdc3f39d5b9ef3ca124675b426df13854631b6fc8f076c8fd`.
Worker использует стандартный JSON с пробелами и ORDER BY type,name;
runtime использовал compact JSON и дополнительную сортировку tbl_name.
Нельзя подменять CI fingerprint или продолжать реальный case с локальным hash.

Исправление ограничено `runtime_schema.schema_fingerprint`: точные поля,
порядок строк, tuple и стандартный JSON совпадают с уже pinned worker.
Общая `_digest` и `migration_sql_fingerprint` не изменены; trusted worker и
policy SHA не меняются. Поведенческий regression test создаёт настоящую
migrated SQLite, Unicode table, UNIQUE autoindex, sequence, index, view,
trigger и sqlite_stat1; проверяет равенство worker/runtime, независимость от
данных, чувствительность к DROP INDEX и сохранение compact SQL hash.

## Следующее условие допуска

Нужны review исправления, новый merge SHA и полный CI точной пары. Затем заново
получить native CI receipt, exact snapshot и image/OCI нового SHA, повторить
Linux synthetic с равенством CI/runtime и отдельно проверить настоящий
migration broker на writable закрытой копии. Старые receipts/image выше
относятся только к 595d143 и на новый SHA не переносятся. Harness constants
до нового успешного CI сохраняют исторический SHA; запускать их как новый
допуск нельзя. Остановка обоих каналов требует отдельного review родителя.

Последний baseline 15:57:11 UTC: checkout previous SHA чистый; оба прежних
container IDs running/healthy, restarts=0; service/timer active; marker,
active-db.json, controller.sha и recovery-blocked отсутствуют. State SHA256
`fc0036ce799f6a850b4c0a6702aeda711870aea15456e7ede03b7c284f6ad317`.
Production SQLite не открывалась; production units/controller/state/marker
не менялись. Docker изменён только private image build/cache/tag, без запуска
контейнера. Боты не останавливались и не перезапускались.

Локальное исправление проверено явным проектным venv Python 3.12:
`-B -m unittest tests.test_runtime_schema tests.test_deploy_database_switch
tests.test_repository_compatibility_contract tests.test_deploy_rollout` —
**36 tests OK, 38.338 с**. Первый запуск ошибочно включал несуществующий
`tests.test_deploy_preflight`: 26 настоящих tests прошли, loader дал один ERROR;
повтор выше использует существующие модули и успешен. Полная локальная матрица
не повторялась. Native Linux proof нового исправления ещё не выполнен: нужен
новый exact merge SHA/полный CI. Commit/push исполнитель не делал.
