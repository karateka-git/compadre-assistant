# Шаг 7.7: база полного CI при опубликованном, но не установленном выпуске

Этот протокол описывает исправление выбора предыдущей версии для полного CI.
Практический порядок закрепления базы после установки находится в
[OPERATIONS.md](../OPERATIONS.md#закрепить-базу-полного-ci-после-установки),
условия выпуска — в [RELEASES.md](../RELEASES.md#2-получить-полный-ci-для-точного-коммита).

## Зафиксировать расхождение публикации и установки

06.11.2026 опубликован immutable Release v3.2.0 с SHA
`cabc0e24d28947123868db8a60f6b55095feb63e`, но VPS продолжает работать
на v3.1.0 с SHA `dd71c2ba42a0744b5b654cedcf73009ce8879058`. Старый
`full_ci_controller` выбирал последний опубликованный Release и поэтому после
публикации передавал в `full-suite` базу v3.2.0. Доверенная CI policy на
установленную v3.1.0 отвергала эту пару. Ротация policy на v3.2.0 давала
успешный полный CI, но для будущего deploy следующей версии его evidence не
совпало бы с `last_good` на VPS. Эта неоднозначность устраняется в этапе 7.

## Читать установленную базу из внешней policy

`main-full` и `release-full` передают контроллеру внешнюю repository variable
`COMPATIBILITY_POLICY_SHA`. Контроллер читает policy JSON по точному SHA,
проверяет её repository, previous SHA, Release tag/ID, immutable Release и
прямой tag. Только проверенный previous SHA передаётся в `full-suite`.
Отсутствующий, повреждённый или чужой pin закрывает полный CI. Candidate-local
policy и последний опубликованный Release не выбирают базу. Доверенный
`repository-compatibility` job повторно сверяет те же SHA, Release и tag и
создаёт evidence точной пары; predeploy verifier сверяет его с фактическим
`last_good` и отдельным production pin.

Для текущего VPS reviewed CI policy должна задавать v3.1.0, SHA
`dd71c2ba42a0744b5b654cedcf73009ce8879058`, Release ID `431378458`.
Опубликованный runtime v3.2.0 и его ранее полученный допуск остаются теми же.
Его production external pin `eb1dad4fee8a176ec4870db12da5e1051a3d1c6e`
не ротируется при этой правке CI. После подтверждённого deploy v3.2.0 новая
отдельная reviewed policy закрепит его как установленную базу для следующего
кандидата; только тогда меняется repository variable. Если новый кандидат
появится до установки v3.2.0, его CI сравнивает с v3.1.0.

## Проверить и допустить изменение

Локально проверить выбор `main` и `release` для опубликованной, но не
установленной v3.2.0; отказ при пустом/некорректном pin, повреждённом
Contents, чужом repository, Release или tag. Затем нужны `pr-fast`, merge
commit, перемещение repository variable на **точный reviewed merge SHA** и
новый полный `main-full` этого SHA с успешными `repository-compatibility` и
`full-suite`. Изменённые workflow должны побайтно совпадать с workflow по
новому reviewed policy SHA. После полного CI собрать exact controller
snapshot и проверить на закрытом Linux. До отдельного review не менять
установленный production controller, units, token, policy или runtime.

До PR из закреплённого worktree запущены 63 теста контроллера CI,
policy contract, predeploy и публикации через проектный venv: **OK**.
Проверка подтверждает, что оба caller обращаются к Contents API с exact
внешним pin. `git diff --check` успешен. Это локальные результаты; полный
CI exact merge SHA и Linux snapshot будут записаны после получения.
