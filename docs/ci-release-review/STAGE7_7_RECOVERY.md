# Шаг 7.7: восстановить выпуск v3.2.0 вперёд

Этот протокол фиксирует единственный запуск root-only `scripts.deploy_forward_recovery`
для выпуска v3.2.0 после [отказа штатного deploy](STAGE7_7_DEPLOY_INCIDENT.md).
Текущий снимок VPS приведён в [PRODUCTION.md](../PRODUCTION.md). Повтор этой
команды без нового допуска не разрешён.

## Проверить допуск

06.11.2026 до запуска перечитан `docs/PRODUCTION.md`. Под существующим
`deploy.lock` заново подтверждены `degraded`, origin `finalization_intent`,
admission=true, selector NEW, marker `critical`, выключенный backup timer,
основной service `failed`, оба exact candidate ID `exited`/restart=no и
отсутствие других Docker writers. `Host.release_api` выбрал приватный
root:root/0600 GitHub token; анонимный API не использовался. Controller pin
`aeeb73c557face466ce411b8c22a4aad04750861` и точные main/backup units
прошли `require_infrastructure`.

Установленный контроллер повторно сверил опубликованный Release v3.2.0,
SHA `cabc0e24d28947123868db8a60f6b55095feb63e`, успешный CI
`42249037867/1`, artifact и evidence для предыдущего установленного SHA
`dd71c2ba42a0744b5b654cedcf73009ce8879058`. Внешний production policy pin
оставался `eb1dad4fee8a176ec4870db12da5e1051a3d1c6e`. Checkout был чистым
на exact candidate SHA. Boot, image/OCI и два ID совпали с durable state.

Обе SQLite прошли integrity/FK/schema и имели прежний logical digest
`49103ef442d3119678762c39c9707c555a10f7670a4d4cb7218a581c05ca50c1`
при сериализации `iterdump` с завершающим LF. Final snapshot и отдельный
backup проверены на существование, integrity и FK; hash final snapshot
совпал с durable записью. Строки БД не выводились.

## Выполнить один recovery

В 20:16:07 UTC запущена единственная команда:

```bash
sudo env -i HOME=/root PATH=/usr/bin:/bin \
  PYTHONPATH=/usr/local/lib/compadre-assistant-deploy \
  /usr/bin/python3 -P -B -m scripts.deploy_forward_recovery
```

Она завершилась с кодом 0 в 20:17:13 UTC, примерно за 66 секунд.
Durable переходы: `forward_recovery_intent` → `forward_recovery_verified` →
`finalization_intent` → `runtime_adopted` → `success`. Вручную state, marker,
selector и SQLite не правились; новые контейнеры и поколения БД не создавались.
CI policy baseline на этом шаге не ротировался.

## Независимо проверить результат

Отдельный SSH-процесс подтвердил `stage=success`, `last_good=v3.2.0`/exact SHA,
selector NEW, отсутствие marker, active/enabled main service и backup timer,
backup service inactive. Те же TG/VK ID работают на exact image, оба `healthy`,
restarts 0, restart policy `no` под управлением systemd. В ограниченном окне
найдены polling signals обоих каналов без `Traceback`/`Conflict`; дублей и
сторонних Docker writers нет.

Прежняя и новая SQLite сохранили integrity=ok, FK 0, fingerprint схемы и
исходный logical digest. Final snapshot из `last_good.backup_id` существует,
имеет root/0400, hash из durable state, integrity=ok, FK 0 и тот же logical
digest. Значения строк и полные журналы не выводились.

В исходной команде deploy интервал `deploy.stop_both` начался в 19:09:15 UTC.
Успех recovery подтверждён в 20:17:13 UTC: наблюдаемое верхнее окно недоступности
до подтверждения составляет около 68 минут. Точное время появления health
между запуском контейнеров в 20:16:24 UTC и завершением команды не измерялось.
Пользователь отдельно подтвердил живые ответы `/help` в Telegram и VK после
восстановления; тексты ответов и идентификаторы чатов не записывались.

Результат независимо сверен по `last_good`, выбранной SQLite, health обоих
каналов и backup. Попытка ручной ротации CI baseline после выпуска была
отменена; текущее состояние CI и остаточный риск следующего выпуска описаны
в [итоговом протоколе 7.7](STAGE7_7_POST_DEPLOY_CI.md). Reboot и потеря
питания единственного VPS не испытывались.
