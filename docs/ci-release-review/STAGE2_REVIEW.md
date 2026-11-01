# Интеграционная проверка этапа 2

На 01.11.2026 локальная интеграционная проверка шагов 2.1–2.5 пройдена;
**критерий всего этапа 2 ещё не выполнен**. Новый CI run, выпуск и установка
с этими метриками не наблюдались. Этот отчёт дополняет [план](PLAN.md#2-оставить-постоянные-метрики-и-короткий-отчёт),
[описание CI](STAGE2_CI.md), [измерения deploy](STAGE2_DEPLOY.md) и
[границы метрик Codex](STAGE2_CODEX.md). Допуск определяется
[RELEASES.md](../RELEASES.md); порядок действий — [OPERATIONS.md](../OPERATIONS.md).

## Что проверено локально

| Область | Результат и граница доказательства |
| --- | --- |
| Workflow YAML | Четыре изменённых workflow разобраны PyYAML 6.0.3. `ci-metrics` зависит от всех проверочных jobs, использует `always()`, read-only permissions и отдельный artifact для run/attempt. Обёртки долгих команд сохраняют ids и deadlines проверок. GitHub ещё не исполнял новую редакцию. |
| Идентичность CI | Collector сверяет repository, workflow, SHA, run/attempt; читает endpoint конкретной попытки. Для PR head SHA, merge checkout и job SHA сохранены отдельно. Чужая попытка или SHA обычного run отвергаются. |
| Отказ и rerun | Failed suite сохраняет измерения с результатом failure; skipped suite имеет отдельный тип, отсутствующие времена — unknown. Перенесённые jobs отмечены и не увеличивают время текущей попытки. Ошибка текущего API даёт безопасную причину и failure, отказ истории сравнения сохраняет текущий отчёт. |
| Секреты | В новых отчётах нет stderr, env, SQL, содержимого SQLite, текстов исключений deploy или тела ответа GitHub. PR collector исполняется из base SHA без сохранённых checkout credentials; token получает только шаг сбора. Это проверка добавленного пути, а не аудит всего прежнего CI. |
| Deploy/recovery | Fake host проверяет успех, build/backup/migrations/verify failures, rollback и degraded; после исключения остаётся финальный отчёт. Отказ writer метрик не запускает rollback. Вызовы, deadlines, marker и решения прежнего recovery сохранены. |
| Причина и простой | Причина — тип исключения и достигнутый stage. Backup/migrations/verify не изобретаются, если не достигнуты. Простой начинается при попытке stop, заканчивается после финального verify и остаётся открытым в degraded; прошедший простой до перезапуска не реконструируется. |
| Долгий этап | Deploy-тест ждёт фонового предупреждения во время build. CI имеет отдельную `ci_watch`: реальный subprocess ждёт её предупреждения, затем завершается кодом 37; CLI отдельно сохраняет код 23. Broken stdout не меняет success, пересылка сигнала и shell status проверены изолированно. Новый GitHub live log и Linux cancellation ещё не наблюдались. |
| Документация | Локальные относительные ссылки и якоря рабочих записей сверены с файлами/заголовками. Изменения не разрешают публикацию или VPS и не объявляют новый выпуск состоявшимся. |

Текущий worktree не содержит `.venv`. Использован явный интерпретатор
`C:/Users/compadre/Documents/obsChatBot/.venv/Scripts/python.exe`;
доступность PyYAML проверена до тестов. Команда из корня worktree:

```powershell
& 'C:/Users/compadre/Documents/obsChatBot/.venv/Scripts/python.exe' -m unittest tests.test_ci_metrics tests.test_deploy_metrics tests.test_deploy_prod tests.test_deploy_prepare tests.test_full_ci_admission tests.test_full_ci_controller tests.test_select_deploy_release tests.test_publish_release tests.test_create_release_branch
```

Результат: **133 tests, OK, skipped=2**. Пропущены платформенные проверки
Linux `flock` и systemd. Тесты не выполняли реальный deploy, VPS, GitHub writes
или публикацию. Парсинг YAML не заменяет исполнение GitHub Actions; новый
bootstrap shell в runner ещё не проверен.

После шага 2.5 выполнен дополнительный targeted suite той же командой с
наборами `tests.test_ci_watch`, `tests.test_ci_metrics`,
`tests.test_full_ci_admission`, `tests.test_full_ci_controller`,
`tests.test_select_deploy_release`, `tests.test_publish_release`,
`tests.test_create_release_branch`: **85 tests, OK**, без skip. Прежний
deploy-код на этом шаге не менялся. Новые тесты запускают безвредные локальные
Python subprocess, не вызывая Docker, сеть или production-команды.

## Исправления по итогам проверки

1. Первый PR с collector и PR к старой release-базе раньше неизбежно получали
   красный `ci-metrics`: в доверенном base SHA скрипта ещё нет. Теперь отдельный
   shell-шаг без token сохраняет явное `unavailable` с run/attempt и SHA,
   а сбор пропускается. Проверки PR сохраняют свои результаты. Подмена базы
   кодом PR не используется; отсутствие данных остаётся видимым.
2. Ошибка запроса исторических runs раньше заменяла полезный Summary текущей
   попытки сообщением об ошибке. Теперь текущие JSON/Markdown сохраняются,
   а недоступное сравнение обозначается отдельно. Ошибка текущей попытки
   по-прежнему закрывает сбор.
3. Успешный и failed run одного типа могли сравниваться как равнозначные.
   Теперь дополнительно совпадают результат и состав/результаты jobs;
   тест исключает сравнение раннего отказа с полным успехом.
4. В шаге 2.5 добавлено живое предупреждение долгих CI-команд через
   `scripts/ci_watch.py`: pip/unit/Compose в PR и pip/unit/smoke/migrations/
   build-healthcheck полного набора. Порог 60 секунд только печатает
   предупреждение, не становится timeout и не меняет результат проверки.

## Что остаётся до завершения этапа

1. После разрешённой доставки изменения проверить новый `pr-fast`, полный
   `main-full`/`release-full`, skipped и отказный CI-сценарий. Сверить SHA,
   run/attempt, перенесённые jobs rerun, Summary и скачиваемый artifact.
   Первый PR со старой базой подтвердит только состояние unavailable;
   для полного отчёта PR нужен следующий run с collector в base SHA.
2. На новом GitHub run подтвердить видимость предупреждения долгой команды
   до её завершения, сохранение failure/success и корректную отмену Linux
   runner. Реализация и локальная проверка завершены; checkout/setup actions,
   контроллеры и hotfix API-проверки live-warning не охватывает. Полный перечень
   покрытия и семантика отдельных команд — в [STAGE2_CI.md](STAGE2_CI.md).
3. Перед отдельно разрешённым deploy установить проверенный controller
   snapshot по [runbook](../OPERATIONS.md#обновить-отдельно-установленный-controller-snapshot),
   включающий новый `scripts/deploy_metrics.py`. Публикация Release сама по
   себе не обновляет отдельно установленный контроллер. Записать SHA обоих.
4. На стенде и при последующем разрешённом выпуске подтвердить права и
   сохранение приватной истории, видимость долгой операции, отказ записи,
   recovery, границы простоя и последний снимок при прерывании процесса.
   Отдельно пройти пропущенные Linux-проверки; локальные fake host не
   доказывают поведение systemd, reboot и диска production.
5. Накопить сопоставимые измерения нового выпуска и внести агрегированные
   вызовы/ожидания по [формату Codex](STAGE2_CODEX.md#записывать-вызовы-инструментов-и-ожидания).
   Tokens desktop-чата остаются `unknown`, пока не подтверждён источник
   именно этого thread. Временную документацию и пункт бэклога сохранять
   до предусмотренной планом эксплуатационной приёмки.
