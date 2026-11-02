# Проверить базовый runtime перед выпуском: шаг 4.4

Независимая проверка от 02.11.2026 подтверждает локальное поведение кандидата
этапа 4 и фиксирует оставшиеся условия выпуска. Контракт описан в
[аудите](STAGE4_AUDIT.md), реализация — в [шаге 4.2](STAGE4_MIGRATION.md)
и [шаге 4.3](STAGE4_UNKNOWN.md). Правила выпуска остаются в
[RELEASES.md](../RELEASES.md), установки и recovery — в
[OPERATIONS.md](../OPERATIONS.md#отказ-выпуска-и-возврат).

## Проверить область результата

Проверены незакоммиченные изменения worktree `ci-release-review-plan/obsChatBot`
ветки `codex/ci-release-review-plan` над HEAD
`8200760a0c5d0a3976cefe7b7cc9cec1fb0fc2c0`. Этот SHA обозначает базу проверки,
а не окончательный выпускной кандидат. Commit, push, PR, публикация Release,
установка и обращения к VPS в шаге 4.4 не выполнялись.

## Подтвердить отказ и доверенную границу

| Проверяемый путь | Результат ревью |
| --- | --- |
| Допуск runtime | Принимается только точное совпадение base/candidate SHA и image ID, controller SHA, verification ID с независимым launch context. Неполные, лишние, повторные поля, иной формат и неверные digest закрывают запуск без fallback. |
| История и схема | Полный базовый журнал и digest точных SQL-файлов совпадают; target строго продолжает базу. Журнал БД и fingerprint всех записей `sqlite_schema` проверяются под `BEGIN IMMEDIATE`. Чужой DDL при том же журнале не получает допуск. |
| Побочные эффекты допуска | Нет создания журнала, применения известного или неизвестного SQL и изменения журнала; при успехе и отказе завершается пустая транзакция. WAL самого подключения не означает абсолютного отсутствия дисковых записей. |
| CLI | Обычный startup, Telegram, VK и process-url используют runtime validator до предметной обработки/polling. `--migrate` сохраняет строгий мигратор независимо от env допуска; автономные smoke также строгие. |
| Выпущенный SQL и recovery | Diff файлов миграций и `scripts/deploy_prod.py` пуст. В том числе `0001_initial_schema.sql` не изменён. Попытка новой миграции по-прежнему закрывает recovery состоянием `degraded`, даже без новой строки журнала. |

JSON не аутентифицирует Docker image. Сверка реально запущенного image ID и
OCI revision, происхождение evidence, защита обоих read-only файлов и запрет
внешнего DDL после validator принадлежат доверенному контроллеру. Его текущий
код не выдаёт и не монтирует production-допуск. Поэтому перечень подтверждённых
production-пар сейчас **пуст**; тестовая пара с `0005_future.sql` подтверждает
контракт, но не совместимость будущего релиза.

## Проверить неизвестные значения и исправить найденное

Проверены все границы из аудита: ArticleStatus, proposal action/status,
LLM action, vault action/status, identity rebind status и GitHub Device Flow.
Свободные строки и внутренние outcomes не объявлены десериализуемыми enum.
UNKNOWN сохраняет исходные данные в DTO/БД и не становится известным lifecycle
или действием. Общие ID/origin/version-инварианты сохраняются; известные
proposal продолжают проверять path/markdown/blob/commit. Неизвестному действию
не приписывается семантика UPDATE, SKIP, APPLIED или замены vault.

Guard article останавливает fetch/LLM/review и запись sentinel. Proposal
confirm/cancel отказывают до claim/lease/GitHub; SQL завершения допускает только
известные действия. Vault service/repository отказывают до lock/CAS/DELETE при
обоих решениях. Identity SQL допускает изменение только `pending`: неизвестное
состояние не проходит CAS, не меняет владельца и не расходует token. Незнакомый
Device Flow error заканчивает локальную попытку без сохранения token или
installations; повреждённый payload остаётся ошибкой формата. LLM UNKNOWN
останавливает генерацию Markdown и сохранение proposal.

Независимое ревью нашло и исправило два пробела presentation:

1. Повтор URL статьи с известным ArticleStatus и неизвестным action/status
   proposal проверял предложение после lease, vault sync и сохранения сообщения.
   Теперь read-only preflight читает статью и активный/исторический proposal до
   этих эффектов. Публичный метод чтения `get_saved_for_url` заменяет прежний
   узкий boolean; callers и тестовые doubles переведены вместе.
2. Нейтральный ответ о неизвестном pending action мог получить кнопки
   подтверждения. Теперь UNKNOWN action не получает такие кнопки.

[Regression-тесты](../../tests/test_unknown_status_boundaries.py) проверяют
неизвестное действие, неизвестный lifecycle и исторический итог при повторе URL:
нет lease, sync, сохранения сообщения и запуска article pipeline. Отдельный тест
проверяет отсутствие кнопок. [Реестр ответов](../user-messages/OBSIDIAN.md)
уточняет эту гарантию. Full suite также обнаружил устаревший fake CLI, который
не реализовывал новое чтение до pipeline; он переведён на текущий контракт.

## Повторить проверки

В worktree `.venv` отсутствует. Использован явный интерпретатор основного проекта
`C:\Users\compadre\Documents\obsChatBot\.venv\Scripts\python.exe`;
импорт `openai` успешно проверен. Команды запускались из корня worktree.

| Проверка | Результат |
| --- | --- |
| `-m unittest discover -s tests -q` | 1036 тестов за 70.943 s, `OK (skipped=2)`. Linux-only flock и systemd проверки пропущены на Windows. |
| `-m unittest discover -s tests -p test_unknown_status_boundaries.py -q` | 8 тестов, OK. |
| `-m unittest discover -s tests -p test_process_incoming_message.py -q` | 38 тестов, OK. |
| `-m unittest discover -s tests -p test_cli_process_url.py -q` | 22 теста, OK. |
| `-m compadre_assistant --sqlite-smoke` | Passed. |
| `-m compadre_assistant --pipeline-smoke` | Passed. |
| `-m compadre_assistant --analysis-smoke` | Passed. |
| `git diff --check` | Нет ошибок пробелов. |
| Относительные ссылки документов этапа 4 и реестра Obsidian | 83 относительные ссылки и их Markdown-якоря проверены, ошибок нет. |

Первый полный прогон: 1034 теста, одна failure CLI из-за устаревшего fake,
2 skipped. Промежуточный прогон до его исправления: 1036 тестов, та же failure,
2 skipped. Итоговый лог: `%TEMP%/obs_stage4_review_verified_unittest.log`.
Эти локальные логи не являются опубликованным CI evidence.

## Сохранить границу следующего шага

Локальная часть 4.4 завершена после исправлений и итоговых проверок. Полный CI
окончательного SHA ещё не подтверждён: незакоммиченный worktree не имеет
выпускного SHA. Сборка точного образа, Linux/container healthcheck и будущий
harness совместимости здесь не выполнялись. До публикации нужен полный
допуск окончательного SHA по RELEASES, а не перенос локального результата.

Критерий всего этапа 4 **не выполнен**: базовая версия ещё не стала фактически
установленной целью возврата. Шаг 4.5 не начат; release и installation остаются
отдельными действиями. По уточнению пользователя работа останавливается на 4.4.
Этап 5 должен подтвердить startup и двусторонние данные точной пары образов;
этап 6 — доставку допуска и безопасный recovery. Текущее ревью не разрешает
возврат старого runtime на новую production-БД.
