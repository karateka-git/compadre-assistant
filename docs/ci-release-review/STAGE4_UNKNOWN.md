# Безопасное чтение неизвестных статусов: шаг 4.3

Запись от 02.11.2026 фиксирует охват и локальную проверку шага 4.3
[плана](PLAN.md#4-подготовить-версию-пригодную-для-будущего-возврата).
Границы выбраны по [аудиту 4.1](STAGE4_AUDIT.md#закрыть-границы-неизвестных-статусов).
Действующие требования к выпуску находятся в [RELEASES.md](../RELEASES.md),
команды установки и восстановления — в [OPERATIONS.md](../OPERATIONS.md).
Эта запись подтверждает поведение исходников и тестов в worktree, а не
совместимость с будущей схемой или установленный выпуск.

## Прочитать незнакомое значение

`UNKNOWN` используется только на границах, где текущая версия читает
строковый enum/status из SQLite DTO или ответа API. Исходная строка остаётся
в DTO и БД. Обратная запись sentinel поверх неё запрещена; production `CHECK`
не менялся.

| Граница | Чтение | Отказ предметного сценария |
| --- | --- | --- |
| `ArticleStatus` | `article_from_dto` переводит новый статус в `UNKNOWN`. | Presentation проверяет статью до vault sync, записи сообщения и article lease; URL reprocess, LLM-анализ и review останавливаются до HTTP/LLM и смены статуса. Repository не записывает sentinel. |
| Proposal action/status | `proposal_from_dto` переводит новое действие и lifecycle-состояние в `UNKNOWN`, сохраняя остальные поля. | Confirm/cancel не захватывают claim или lease. SQL claim, completion и завершение дополнительно ограничены известными действиями. Активная неизвестная запись и исторический итог не выдаются за известный результат; новый `UNKNOWN` proposal нельзя сохранить. |
| LLM action | Строковое неизвестное действие корректного JSON становится `UNKNOWN`-планом. Некорректное поле и JSON остаются ошибкой формата. | Review останавливается до генерации Markdown и сохранения proposal. |
| Vault action/status | `vault_confirmation_from_dto` переводит обе строки в типизированные enum с `UNKNOWN`; известный replacement сохраняется. | Service и repository отказывают до CAS/DELETE при `yes` и `no`. |
| Identity rebind status | `rebind_confirmation_from_dto` переводит новую строку в типизированный `IdentityRebindStatus.UNKNOWN`. | SQL CAS допускает решение только для `pending`; незнакомый статус не меняет identity, confirmation или token. |
| GitHub Device Flow status | Неизвестный непустой строковый `error` API становится `GitHubDevicePollStatus.UNKNOWN`; повреждённый payload остаётся ошибкой формата. | Coordinator завершает попытку `FAILED` после одного poll без сохранения token/installations и без повторного ожидания. |

Для известных значений сохранены прежние проверки domain-моделей. Неизвестные
action/status proposal проходят общие проверки ID, origin и version, но не
получают семантику `ADD`, `UPDATE`, `SKIP` или `APPLIED`. Типизированный статус
vault и identity введён вместе с mapper и callers, без строкового wrapper.

## Проверить границы и результат

Проверки выполнены явным Python проектного окружения
`C:\Users\compadre\Documents\obsChatBot\.venv\Scripts\python.exe`.
Зависимость `openai` доступна; проект использует `unittest` (`pytest` в этом
venv отсутствует).

| Проверка | Результат |
| --- | --- |
| Затронутые тесты: mapper, URL/анализ/review, proposal, vault, identity, GitHub API/flow, LLM и presentation | Последний выборочный прогон: 148 тестов, `OK`; `test_*obsidian*.py`: 36 тестов, `OK`. Проверены отсутствие HTTP/LLM/GitHub вызовов, vault sync, claim/lease/CAS, нейтральный текст исторического итога и неизменность синтетических строк proposal/identity. |
| Полный `python -m unittest discover -s tests -q` | Первый прогон: 1033 теста, 1 ошибка очистки временного `app.db` из-за Windows `PermissionError [WinError 32]`. Повторный прогон до последних guards presentation: 1033 теста, `OK (skipped=2)`; итоговый лог сохранён во временном каталоге как `obs_stage4_unknown_unittest.log`. |
| `git diff --check` | Ошибок пробелов нет. |

Тесты новых значений используют DTO/API и отдельные синтетические SQLite
таблицы для проверки SQL guard. Они не доказывают работу на ещё не созданной
production миграции или совместимость будущего образа с новой схемой.
Проверка точной пары образ/схема и полный CI относятся к шагам 4.4–5;
публикация, push и VPS в 4.3 не выполнялись.
