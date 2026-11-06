# Шаг 7.7: исправление D-Bus допуска контроллера

При подготовке selector units реальный Linux gate отказал до выпуска; причина
и безопасный откат записаны в [инфраструктурном отчёте](STAGE7_7_INFRASTRUCTURE.md).
Этот документ фиксирует узкое исправление **отдельного controller snapshot**.
Опубликованный runtime [v3.2.0](STAGE7_7_RELEASE.md) с SHA
`cabc0e24d28947123868db8a60f6b55095feb63e` не меняется; новые
production units, token/policy и `deploy-prod` до review не устанавливаются.

## Исправить проверку скалярного свойства

На VPS `busctl --json=short` возвращает для `RemainAfterExit` структуру
`{"type":"b","data":true}`. Старый `Host.systemd_property()` требовал
`data` типа list для любого свойства, хотя его контракт предназначен для
массивов (`ExecStartEx`, `EnvironmentFiles`, `Conditions`). Поэтому реальная
проверка `require_systemd_boot()` закрывала selector даже при правильных
effective `ExecStartEx`/`ExecStopEx` и `systemd-analyze verify`.

В `scripts/deploy_prod.py` добавлен отдельный `systemd_bool_property()`:
он принимает только сигнатуру `b` и значение строго типа bool; повреждённый
JSON, другую сигнатуру, список, число или строку отвергает тем же безопасным
`DeployError`. Массивный reader не ослаблен. Проверка `RemainAfterExit`
требует именно `True`, так что валидное `False` также закрывает boot.

`tests/test_deploy_prod.py` проверяет полный selector boot contract с
реальным ответом `data:true` и отказ для `false`, `[true]`, неверной
сигнатуры, числа, строки и отсутствующего поля. Локально из закреплённого
worktree через явный проектный Python
`C:/Users/compadre/Documents/obsChatBot/.venv/Scripts/python.exe` выполнены
`tests.test_deploy_prod.SystemdBootTests tests.test_deploy_rollout`:
**12 tests OK, 1 Linux-only skip**. `git diff --check` успешен.

## Получить новый допуск и snapshot

Локальный результат ещё не является допуском production. Дальше нужны
PR с `pr-fast`, merge commit в `main`, полный `main-full` точного нового SHA,
новый автономный controller archive exact SHA, сверка всех Git-байтов и
private Linux проверка реальной формы `RemainAfterExit`. Только затем
заменить установленный controller snapshot по отдельному review. Release
runtime v3.2.0, его CI и compatibility artifact относятся к прежней паре
`v3.1.0` → `cabc0e2`; SHA контроллера может быть новее и фиксируется отдельно.
