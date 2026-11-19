# Включить читателей и писатель Deployment v2

Этот порядок связывает SHA доверенного CI, изолированного контроллера VPS и
первого настоящего PATCH. Условия продолжения открытой попытки описаны
в [восстановлении](RECOVERY.md), правила выбора базы — в [описании читателя](READERS.md).

## Согласовать окно и отдельные доступы

Владелец выбирает поддерживаемые линии, содержание следующего PATCH и окно
вне штатного backup. Для перехода нужны отдельные согласования на push,
открытие и слияние PR, изменение Actions и pin, установку snapshot и
ограниченного SSH-входа на VPS и запуск production workflow с точными
версией, SHA, `change=patch` и release notes. Полный CI можно подтвердить
только по реальному run/attempt.

Для GitHub нужны права выполнить защищённые PR, запускать полный CI,
менять repository variable и настройки окружения `production-release`.
Секрет `PRODUCTION_RELEASE_TOKEN` должен иметь запись Contents и Deployments,
чтение Actions, Variables, Pull requests и Administration. Если кандидат
затрагивает workflow, требуется разрешение записи Workflows для тега.
На VPS нужен администратор с root для установки snapshot, локального pin,
wrapper и ограничения отдельного ключа runner. Операторский ключ на VPS
и секрет runner имеют разные назначения.

## Подготовить проверенный источник и поставить выпуск на паузу

1. Провести изменения контроллера и читателя через защищённую `main`.
   Получить точный reviewed merge SHA `M`. Из `M` взять
   [`main-full.yml`](../../.github/workflows/main-full.yml) и
   [`release-full.yml`](../../.github/workflows/release-full.yml). Подготовить
   те же байты для отдельных связанных PR в `release/4.2`, `release/4.1`,
   `release/4.0`, `release/3.4` и других выбранных владельцем линиях.
   Все четыре указанные линии имеют те же байты `full-suite.yml`. Для
   другой линии сверить все три workflow с `M` и включить любые требуемые
   изменения той линии в согласованный PR.
2. Перед сменой pin исключить запуск новых production workflow
   и независимые публикации от имени владельца, например временно отключив
   workflow `production-release.yml` на согласованный период. Дождаться
   окончания всех выполняющихся и ожидающих release/resume. Проверить на VPS
   чистый checkout, принятый `last_good`, отсутствие marker и активного
   backup. Сохранить прежние policy pin, snapshot и настройки. При
   незавершённом выпуске сначала разобрать фактический результат.

## Включить читателей в CI и на VPS

1. Установить repository variable `COMPATIBILITY_POLICY_SHA=M` и прочитать
   её обратно. Пока `M` остаётся вершиной `main`, запустить
   `main-full.yml` для `main` с `expected_sha=M` и
   `expected_policy_sha=M`. Если вершина ушла, согласовать новый
   reviewed merge SHA и pin. Дождаться
   успешных controller, compatibility и full-suite. Сверить точные run/attempt,
   SHA, policy, Deployment и статус принятой базы, evidence, отчёт и ZIP.
   При отказе сверить pin, логи и фактическое состояние VPS по
   [порядку возврата policy](../OPERATIONS.md#обновить-доверенную-политику-полного-ci).
2. Синхронизировать согласованные release-ветки и повторить те же проверки
   через `release-full.yml`. В каждом запуске `expected_sha` — полный SHA
   текущей вершины этой линии, `expected_policy_sha=M`. Caller и harness
   независимо подтвердят один Deployment и статус. Пока байты workflow
   линии расходятся с pin, её новый полный CI не даёт допуска. Для первого
   PATCH требуется успешный полный CI **самого кандидата** на `release/4.2`
   с текущей базой. Если вершина изменится после прогона, проверить новую
   вершину.
3. Собрать snapshot из Git-байтов `M`, который прошёл точный полный CI.
   Проверить SHA-256 и изолированные импорты по
   [порядку установки](../OPERATIONS.md#обновить-отдельно-установленный-controller-snapshot).
   В согласованное окно под существующим `deploy.lock` установить snapshot,
   root-wrapper и `controller.sha=M`, установить
   `/var/lib/compadre-assistant-deploy/compatibility-policy.sha=M` и сверить
   приёмку. Режим v2 зашит в snapshot и выбирается после `env -i`.
   В окружении `production-release` установить `PRODUCTION_CONTROLLER_SHA=M`.
   Проверить, что production runner получит код проверенного `main`,
   а фактически установленный snapshot равен pin.
4. Настроить на отдельной учётной записи runner для его ключа
   `restrict,command="/usr/bin/python3 -I /usr/local/lib/compadre-assistant-deploy/deploy/attempt-ssh.py"`
   и ограниченный `sudo -n /usr/local/sbin/deploy-prod` для точной команды
   `--attempt`. Сверить владельцев и права файлов. Убрать действующий
   доступ runner через `compadre-release-entry` и его sudo-разрешение на
   `compadre-release-ssh`. Исключить ручных писателей до открытия
   приёмки. Повторить read-only
   сверку текущей установки, обоих каналов, незанятой секции backup и
   прав штатного GitHub read token.

## Запустить согласованный первый PATCH

1. Проверить подходящий реальный PATCH, вершину `release/4.2`,
   последовательный свободный тег, подтверждённые связи с `main` и
   evidence полного CI с `M`. Если кандидата или согласования ещё нет,
   оставить писатель выключенным для новых v2.
2. В том же согласованном окне проверить результаты v1, v2 и прямое
   чтение принятой базы в секции, описанной
   [в сверке читателей](READERS.md#сверить-стартовую-базу-при-переключении).
   Сохранить исключение других писателей до подтверждённого открытия
   первого D18. Выставить `PRODUCTION_DEPLOYMENT_WRITER=v2` в
   `production-release`, прочитать pin и writer обратно, разрешить
   согласованный запуск и выполнить штатный `release` на защищённой `main`.
   `source_attempt` передаётся при `resume` после обрыва исходного запуска.
3. При резервировании контроллер должен прочитать активный run/attempt
   production workflow штатным VPS token и сверить его владельца.
   Ошибка оставляет установку закрытой. Если Release уже опубликован,
   сверить его с кандидатом и сохранить для последующего допуска.
   После открытия D18 сохранить
   его точный ID, установочный run/attempt и результат каждой стадии.
   Проверить журналы и расход REST/GraphQL, новый текущий статус после
   очистки, совпадение Release, SHA, поколения и миграции на VPS, backup и
   работу двух каналов. Владелец отдельно подтверждает живые сценарии в
   Telegram и VK. При обрыве использовать
   [правила восстановления](RECOVERY.md) и исходный ключ попытки.

После появления D18 сохранять читателей v2. Чтобы заблокировать новые
попытки и продолжить открытую, использовать `resume-only`.
При возврате прежнего policy до первого D18 синхронизировать байты
workflow уже обновлённых линий с возвращённым pin и повторить точный
полный CI. До этого сохранить паузу production.

## Результат чтения 19.11.2026

Текущая вершина `release/4.2` совпала с commit тега последнего immutable
Release `v4.2.4` (`448512857`,
`5edb92b7b0dbbaec0270889a8a34849eee77580c`). Открытых PR и
выполняющихся либо ожидающих production runs на момент сверки не было.
Подходящего следующего PATCH на вершине нет.
[Release v4.2.4](https://github.com/karateka-git/compadre-assistant/releases/tag/v4.2.4)

SSH администратора с проверкой host key и `sudo -n` на VPS прошли.
Штатный `--verify-installed` подтвердил чистый detached checkout, stage
`success`, принятое поколение `0abfa816100242429b9682e7e9231001`,
миграцию `0007_family.sql` и оба healthy-контейнера. Основной service и
backup timer активны, backup service `inactive` с `Result=success`.
Установленный snapshot — `6de377cf402483c32faa63b805f6420df434fe70`,
`attempt-ssh.py` в нём отсутствует. Общий pin GitHub и VPS —
`e65ba164780d066eebfdfff0f335bbbf81cbf1e8`. В окружении
`production-release` есть штатные переменные хоста и имена двух секретов.
`PRODUCTION_CONTROLLER_SHA` и `PRODUCTION_DEPLOYMENT_WRITER` отсутствуют
как в окружении, так и на уровне репозитория. Правило окружения разрешает
только `main`.

У выделенной учётной записи runner один ключ с `restrict` и текущим
forced command `/usr/local/libexec/compadre-release-entry`. Под этой
учётной записью вызов серверной цепочки v1 с `--verify-installed`
подтвердил тот же приёмочный результат. Проверены права на старый
root dispatcher `compadre-release-ssh`. Прямой произвольный sudo и
разрешение v2 `--attempt` отсутствуют. Вход через сам секретный
ключ Actions проверяется в согласованном запуске.

Штатный VPS `github-read-token` вернул HTTP 200 для двух завершённых
точных попыток Actions и для GraphQL `nodes(ids:)` всех 16 исторических
Deployment. Локальный архив контроллера прошёл сверку 75 файлов и
изолированных импортов. Прочитанный в памяти VPS с тем же token и
`env -i` читатель из этого архива выбрал ту же базу за 22 REST и 2 GraphQL.
State и оба локальных pin до и после чтений совпали. Проверку активной
production попытки выполнить при первом реальном резервировании.
[Выкладка 44070943979/1](https://github.com/karateka-git/compadre-assistant/actions/runs/44070943979/attempts/1) ·
[Полный CI 44069516542/1](https://github.com/karateka-git/compadre-assistant/actions/runs/44069516542/attempts/1)

Локальный запуск точного шага `Decide full run` на checkout текущих вершин
`release/3.4`, `release/4.0`, `release/4.1` и `release/4.2` с отдельно
закреплённой политикой выбрал `v4.2.4` реальным GitHub API. На каждой
ветке отдельный процесс загрузил читатель v2 из policy, несмотря на
`PYTHONPATH`, указывавший на checkout кандидата. Несовпадение ожидаемого
pin или HEAD политики завершало шаг без записи CI outputs. Для новых
PR и их exact SHA предстоят штатные полные CI в GitHub.

Новый читатель в замороженном режиме повторно принял аннотации и
исторические evidence всех четырёх линий по их исходным policy SHA.
Проверены точные Release и run/attempt, архив, SHA-256, байты workflow
и policy и сохранённая база из Deployment.

| Выпуск | Подтверждённый полный CI | Artifact ID |
| --- | --- | ---: |
| `v3.4.0` | [43194817952/1](https://github.com/karateka-git/compadre-assistant/actions/runs/43194817952/attempts/1) | 13933085460 |
| `v4.0.6` | [43714090269/1](https://github.com/karateka-git/compadre-assistant/actions/runs/43714090269/attempts/1) | 14161529455 |
| `v4.1.1` | [43873138601/1](https://github.com/karateka-git/compadre-assistant/actions/runs/43873138601/attempts/1) | 14229480018 |
| `v4.2.4` | [44069516542/1](https://github.com/karateka-git/compadre-assistant/actions/runs/44069516542/attempts/1) | 14320819796 |

Для вершины `release/3.4` на SHA
`efe22a4f019e68a0436ee3d5e1825ab9248934e0` проверен её прежний полный CI
с `v3.4.0` в качестве базы, с полным evidence в artifact `14015696076`.
[Полный CI 43378457266/1](https://github.com/karateka-git/compadre-assistant/actions/runs/43378457266/attempts/1)
