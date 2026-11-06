# Шаг 7.7: инфраструктурный gate остановил подготовку

Этот отчёт фиксирует подготовку private policy/token и selector units после
[установки контроллера](STAGE7_7_CONTROLLER.md). Попытка выявила реальное
несоответствие D-Bus контракта; изменения units и private файлов откатились.
`deploy-prod v3.2.0` не запускался, production БД и боты не переключались.

## Проверить исходное состояние

06.11.2026 в 18:10:35 UTC заново подтверждены clean checkout прежнего
`dd71c2ba42a0744b5b654cedcf73009ce8879058`, `stage=success`, прежний
state SHA256 `fc0036ce799f6a850b4c0a6702aeda711870aea15456e7ede03b7c284f6ad317`,
отсутствие marker/selector/recovery-blocked, controller pin exact
`cabc0e24d28947123868db8a60f6b55095feb63e`, inode `deploy.lock=279276`.
Service/timer active, backup service inactive, `NeedDaemonReload=no`, оба
прежних container IDs healthy/restarts0. SQLite read-only: integrity ok,
FK0, migration journal 1–4. Старый main drop-in SHA256
`ef4f911ef49c96f1b9d7bba1698544e3f8a4332125bbbee7ecf88324906369c4`;
backup selector отсутствует, оба marker guard точными байтами совпали с
установленным verified snapshot. Policy/token files отсутствовали.

## Попытаться установить и закрыть gate

Пользователь подготовил отдельный fine-grained read-only GitHub credential;
его Actions/Contents доступ проверен родителем без вывода значения. В одном
SSH-процессе credential передан **только через stdin** под существующим
`deploy.lock`: его значение и hash не попадали в argv, временный файл,
output или этот отчёт. Скрипт записал private token и reviewed policy SHA
`eb1dad4fee8a176ec4870db12da5e1051a3d1c6e` как root:root `0600`.
Прежний main drop-in сохранён в отдельном root-owned private каталоге;
установлены exact template байты основного и backup selector, затем выполнен
`systemctl daemon-reload`.

Проверка `deploy_rollout.require_infrastructure` отказала с `DeployError`.
Процедура под тем же lock восстановила прежний main drop-in, удалила новый
backup selector и оба только что созданных private token/policy файла,
повторила `daemon-reload` и проверила прежних ботов. Token не оставлен во
временных файлах. Контроллер и его pin не откатывались и не менялись.

Диагностическое повторное применение **только двух units, без token** под
lock показало: `systemd-analyze verify` rc0; effective `ExecStartEx`,
`ExecStopEx` основного service и `ExecStartEx` backup service совпали с
selector templates и флагом `no-setuid`; порядок drop-ins правильный,
`NeedDaemonReload=no`. Затем units снова откатились. Отказ находится в
`Host.require_systemd_boot`: реальный `busctl --json=short` для scalar
`RemainAfterExit` возвращает `{"type":"b","data":true}`, тогда как
`Host.systemd_property()` в `scripts/deploy_prod.py` требует `data` типа
list для **всех** сигнатур, а `require_systemd_boot()` ожидает `[True]`.
Из-за этого реальный selector boot gate всегда закрывается на данном VPS.
Фейковый D-Bus ответ тестов использовал список и не выявил расхождение.
Это кодовый отказ; менять systemd response или обходить gate нельзя.

## Подтвердить откат и условие продолжения

Отдельная проверка в **18:15:02 UTC**: прежний main drop-in имеет тот же
SHA256, backup selector отсутствует, private token/policy и token temp
отсутствуют, `NeedDaemonReload=no` всех трёх units. State hash прежний,
marker отсутствует, controller pin exact candidate. Service/timer active,
backup service inactive; Telegram и VK — прежние точные IDs и old image,
оба **healthy**, restarts=0. Production SQLite повторно проверена:
integrity ok, FK0, schema 1–4, WAL0. Выпуск и backup не запускались.

**Gate не пройден.** Нужно узкое исправление обработки scalar D-Bus `b`,
поведенческий тест с настоящей формой `data:true`, новый review/CI и новый
exact candidate/snapshot. Опубликованный immutable `v3.2.0` и его SHA нельзя
исправить на месте; дальнейший релиз требует нового имени и допуска. До
этого units/token/policy и настоящий `deploy-prod` не устанавливать повторно.
