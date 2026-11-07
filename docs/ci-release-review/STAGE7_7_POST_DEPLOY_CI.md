# Шаг 7.7: итог выпуска и состояние CI после восстановления

Этот протокол сводит результат production выпуска v3.2.0 и состояние CI после
него. Подробные доказательства отказа штатной команды и восстановления
содержатся в [протоколе инцидента](STAGE7_7_DEPLOY_INCIDENT.md) и
[протоколе recovery](STAGE7_7_RECOVERY.md); текущий снимок VPS — в
[PRODUCTION.md](../PRODUCTION.md). Запись не даёт допуск следующему выпуску.

## Подтвердить установленную версию

06.11.2026 штатный `sudo deploy-prod v3.2.0` завершился отказом на стадии
`finalization_intent` и оставил `degraded`. После исправления и отдельной
установки controller snapshot один root-only `scripts.deploy_forward_recovery`
завершился с кодом 0 в 20:17:13 UTC. Независимая проверка подтвердила
`stage=success`, `last_good=v3.2.0` на SHA
`cabc0e24d28947123868db8a60f6b55095feb63e`, selector NEW, целые
данные/backup, отсутствие marker, active/enabled main service и backup timer.
Те же exact TG/VK container IDs работали на проверенном image, оба были
`healthy` с restarts 0 и polling signals. Пользователь отдельно подтвердил
живые ответы `/help` в Telegram и VK. Тексты ответов и ID чатов не сохранялись.
Дополнительный read-only осмотр 07.11.2026 в 06:07:35 UTC подтвердил только
checkout exact SHA, active main service/timer и оба прежних ID `healthy` с
restarts 0; SQLite и durable state при нём повторно не проверялись.

Recovery длился около 66 секунд. От начала `deploy.stop_both` в 19:09:15 UTC
до подтверждённого success в 20:17:13 UTC верхнее наблюдаемое окно
недоступности составило около 68 минут; точный момент восстановления health
внутри этого окна не измерялся. Исходная команда deploy не была успешной.

## Зафиксировать результат ротации базы CI

После установки отдельный PR #78 попытался сделать v3.2.0 базой следующего
полного CI. Эта ротация отменена PR #79; repository variable
`COMPATIBILITY_POLICY_SHA` возвращена к
`09f15a5b3c01ec73d02379b918104e8cf697b4b2`. Отдельный production
`compatibility-policy.sha` остался на
`eb1dad4fee8a176ec4870db12da5e1051a3d1c6e` для evidence исходной пары
v3.1.0 → v3.2.0. Успешный автоматический
[main-full 42344876362/1](https://github.com/karateka-git/compadre-assistant/actions/runs/42344876362)
проверил точный SHA `a44cd3c73afefc30b832051fc95fff0e5555142a`:
`main-full-controller`, `repository-compatibility`, `full-suite` и
`ci-metrics` завершились успешно. Этот run подтверждает CI после возврата
policy, но не совместимость будущего кандидата с уже установленной v3.2.0.

Следующий deploy требует отдельного точного допуска от фактического
`last_good` на VPS; текущий CI pin указывает на более старую базу и не может
заменить эту проверку. Автоматизация связи installed версии с CI, подробный
разбор verifier и правила хранения старых SQLite перенесены в
[бэклог](../BACKLOG.md#автоматизировать-сопровождение-выпуска-и-проверить-допуск-ci).
Отмена ротации CI не меняла production runtime, units и рабочую SQLite.

## Сохранить остаточный риск

Linux process crash, startup и прерывания controller на закрытых копиях
проверялись в 7.2–7.5; настоящий systemd/start/health/polling проверен в 7.7.
Reboot и потерю питания единственного production VPS не выполняли по решению
пользователя. Их поведение остаётся непроверенным; process crash не является
их эквивалентом. Перед следующим выпуском нужен свежий осмотр VPS и отдельный
допуск exact previous/candidate пары. Родительский review подтвердил критерий
7.7 по независимой проверке установленного SHA, обоих каналов и данных;
шаг завершён. Следующий этап — сводка и перенос действующих правил.
