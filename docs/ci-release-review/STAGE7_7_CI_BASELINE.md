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
внешним pin. `git diff --check` успешен.

PR [#67](https://github.com/karateka-git/compadre-assistant/pull/67) прошёл
`pr-fast` и слит merge commit
`09f15a5b3c01ec73d02379b918104e8cf697b4b2`. В этом commit policy
содержит v3.1.0/`dd71c2ba42a0744b5b654cedcf73009ce8879058`/Release ID
`431378458` и обе новые workflow версии. Repository variable затем
переведена на exact SHA этого reviewed commit; readback совпал. Production
external pin для runtime v3.2.0 не менялся.

Ручной [main-full 42273239857/1](https://github.com/karateka-git/compadre-assistant/actions/runs/42273239857)
завершился **success** на exact SHA `09f15a5b3c01ec73d02379b918104e8cf697b4b2`:
controller, `repository-compatibility`, `full-suite` и `ci-metrics` успешны.
Compatibility artifact ID `13533115438`, digest
`sha256:c5e8cd1f17d4a3dc57b703c4edc0da77d586e10847939933dd4caa5381e1d6a2`;
на момент проверки `expired=false`. Это CI evidence для будущей пары с
установленной v3.1.0, а не новое evidence ранее опубликованного runtime
v3.2.0. После изменения production `last_good` CI baseline потребуется
ротировать по [порядку](../OPERATIONS.md#закрепить-базу-полного-ci-после-установки).

Из exact merge SHA локально собран archive SHA256
`65a024d790a189344be06b4f5a33432b212dcab280b00a3ac72b0bcfce6d1dd3`:
63 файла, manifest и Git-байты проверены. На VPS архив размещён **только**
в закрытом каталоге
`/var/lib/compadre-assistant-stage7-20261106/controller-09f15a5-proof`.
Linux staging имеет `root:root`/0700, archive `root:root`/0600;
`controller_snapshot unpack` и `verify --require-root` подтвердили 63 файла,
права и изолированные imports. Код из private snapshot прочитал реальное
скалярное D-Bus свойство `RemainAfterExit` как `true`. Установленный
production `controller.sha` остался `cabc0e24d28947123868db8a60f6b55095feb63e`,
основной service и backup timer active, marker отсутствует. Production
Telegram и VK сохранили `healthy` и `restarts=0`. Production
controller/units/token/policy/runtime/БД не переключались; `deploy-prod`
не запускался. Для установки нового snapshot требуется отдельный review.
