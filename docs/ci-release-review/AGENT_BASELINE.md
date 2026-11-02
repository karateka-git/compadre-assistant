# Базовая запись: annotation и работа агента

Проверка от 01.11.2026 разбирает красную annotation в полном CI и границы доступных исторических метрик агента для пути PR → CI → Release → deploy. Маршрут и условия допуска описаны в [RELEASES.md](../RELEASES.md), эксплуатационные действия — в [OPERATIONS.md](../OPERATIONS.md), план замеров — в [PLAN.md](PLAN.md#шаги-этапа-1).

## Разобрать annotation

| Источник | Подтверждённое наблюдение |
| --- | --- |
| [Завершённый `main-full`, run 41005012029, attempt 1](https://github.com/karateka-git/compadre-assistant/actions/runs/41005012029) | SHA `5a8676a41a4eb26f24ed3eea2c3f3dc76408a32c`, 30.10.2026, статус `completed`, итог `success`; job `main-full / full-suite` (ID `123897893188`) тоже `success`. |
| [Лог job, шаг `Run full unittest suite`](https://github.com/karateka-git/compadre-assistant/actions/runs/41005012029/job/123897893188) | В `04:50:04Z` после `Pending input failed: owner_id=selected` показан traceback из `dispatcher.py:721` до `tests/test_pending_input.py:44`; строка `raise RuntimeError('Ошибка внешнего действия')` имеет префикс `##[error]`. В `04:50:38Z`: `Ran 983 tests in 41.727s`, затем `OK`. |
| [Тест на точном SHA, строки 40–45](https://github.com/karateka-git/compadre-assistant/blob/5a8676a41a4eb26f24ed3eea2c3f3dc76408a32c/tests/test_pending_input.py#L40-L45) и [189–195](https://github.com/karateka-git/compadre-assistant/blob/5a8676a41a4eb26f24ed3eea2c3f3dc76408a32c/tests/test_pending_input.py#L189-L195) | `InputCapability.continue_input` намеренно бросает `RuntimeError`, когда включён `raise_error`. `test_accepted_error_and_unexpected_error_clear` проверяет возвращённую ошибку и удаление pending input для обоих вариантов. |
| [Обработчик на точном SHA, строки 720–728](https://github.com/karateka-git/compadre-assistant/blob/5a8676a41a4eb26f24ed3eea2c3f3dc76408a32c/compadre_assistant/core/presentation/incoming/dispatcher.py#L720-L728) | `except Exception` закрывает ожидание, `LOGGER.exception` печатает traceback и возвращает `IncomingResponse` с объектом ошибки. |

Следовательно, красная annotation отражает запись ожидаемого исключения в журнале Actions, а не провал assertion или job. Тест проверяет именно этот путь и проходит; полный unittest завершился `OK`. Наличие красной annotation само по себе не меняет результат допуска CI. Лог не показывает, почему runner выбрал префикс `##[error]` только для этой строки traceback; механизм отображения здесь не установлен.

## Оценить исторические метрики агента

Таблица относится к **историческому** пути до этой проверки, а не к числу read-only вызовов, сделанных при подготовке записи. GitHub run и job фиксируют CI, но не являются журналом действий Codex и не показывают, кто вводил команды, сколько раз клиент повторял API-запрос и сколько агент ожидал ответа.

| Метрика | Исторически подтверждено | Значение для PR → CI → Release → deploy |
| --- | --- | --- |
| Ручные действия | [RELEASES.md](../RELEASES.md#маршрут-выпуска) отделяет merge, публикацию Release и `deploy-prod`; [OPERATIONS.md](../OPERATIONS.md#подготовка-и-запуск-версии) задаёт проверки и команду установки. Это описание требуемых шагов, а не протокол фактически выполненных человеком или агентом команд. Из завершённого [run 41005012029](https://github.com/karateka-git/compadre-assistant/actions/runs/41005012029) нельзя посчитать ручные действия. | **unknown**: нет сопоставимого журнала действий на всём пути. |
| Повторы запросов GitHub API агента | GitHub Actions хранит run/attempt и job, но не историю запросов клиента Codex/`gh` для этого выпуска. Сообщения о `retry` в полном unittest относятся к тестируемому поведению приложения и не доказывают повтор GitHub API агентом. | **unknown**: число повторов и их причины не восстановлены. |
| Ожидания агента | Timestamps Actions описывают выполнение CI, а не начало/конец ожидания агентом. Нет связанной хронологии опросов, пауз и ответов инструментов для PR → CI → Release → deploy. | **unknown**: число ожиданий и их суммарная длительность не восстановлены. |
| Codex tokens desktop-чата | [Официальная документация OpenAI Docs по app-server](https://learn.chatgpt.com/docs/app-server#events) описывает событие `thread/tokenUsage/updated` для активного чата; [профиль desktop-приложения](https://learn.chatgpt.com/docs/app/settings#profile) показывает суммарную token activity. [Счётчик `turn.completed` в `codex exec --json`](https://developers.openai.com/blog/eval-skills) относится к CLI-прогону. Эти источники не предоставляют здесь проверенное значение именно исторического desktop-чата данного выпуска. | **unknown**: счётчик конкретного чата не получен. |

Для следующего сопоставимого выпуска счёт действий, retries и ожиданий нужно привязывать к явным событиям агента с временем и этапом, отдельно от timestamps CI. Токены записывать только при наличии проверенного счётчика соответствующего desktop-чата; профильные суммы и метрики другого CLI-прогона подставлять нельзя. Сессионные данные и секреты в эту запись не переносились.
