# Проверить механизм раздельных сред: шаг 5.2

Документ описывает локальный runner шага 5.2 и границу его результата.
[Аудит 5.1](STAGE5_AUDIT.md) определяет исходный набор старых tests;
[план](PLAN.md#шаги-этапа-5) оставляет полную матрицу, доверенный CI и predeploy
шагам 5.3–5.5. Этот runner пока не подключён к workflow и не выдаёт admission.

## Запустить проверку точной пары

Из корня worktree вызвать проектный Python, передав два полных lowercase SHA
и новый каталог результатов. Каталог результатов не должен существовать:
это исключает смешивание журналов разных прогонов. Объекты обоих commits
должны быть доступны в локальном Git. Runner не делает fetch, не выбирает
release/tag и не меняет рабочий checkout или Git refs.

```powershell
& 'C:/Users/compadre/Documents/obsChatBot/.venv/Scripts/python.exe' scripts/check_repository_compatibility.py --previous-sha dd71c2ba42a0744b5b654cedcf73009ce8879058 --candidate-sha 8980871a8db9583830ba3d537108c731f1e4cb9b --output data/step52-run4
```

Это воспроизведение механизма на паре из аудита, а не указание следующей
production-версии. Выбор доверенного previous release и связь с CI run
будут проверяться отдельно в 5.4. На Linux можно вызвать тот же script
явным Python проектного/CI venv. Windows проверен локально; Linux ещё не
проверен. `--wheelhouse <каталог>` включает `--no-index --find-links` для
установки из заранее подготовленных wheels. Без него pip использует индекс.

При успехе exit code равен 0, при отказе — 1. Читать `report.json` вместе
с `candidate.json`/`candidate.log`, `behavior.json`/`behavior.log`,
`seed.json`/`seed.log` и двумя `*-install.log`. Отказ подготовки оставляет
наборы в `not_run`; ошибка candidate worker не отменяет запуск old behavior.
Отсутствующий результат, timeout, import error, ошибка tests или SQL дают
неуспешный результат. Для исправленного запуска нужен новый output-каталог.

## Проверить, какие bytes исполнялись

[check_repository_compatibility.py](../../scripts/check_repository_compatibility.py)
извлекает каждый tracked blob через `git ls-tree` и `git cat-file --batch`,
сверяя Git blob hash и сохраняя SHA256 manifest. Это обходится без
`core.autocrlf`, checkout filters, `export-subst` и `export-ignore`.
Symlink, submodule, небезопасный путь и коллизия имён filesystem отклоняются.
Untracked файлы, текущие правки worktree и `.env` не становятся источником
кандидата. Внешние runner/worker фиксируются отдельными SHA256.

Previous/candidate получают разные каталоги и новые venv без
`system-site-packages`; каждый устанавливает requirements собственного SHA
и проходит `pip check`. Среды и checkout удаляются при завершении, журналы
остаются в output. Python/SQLite/dependency versions и пути imports входят
в отчёт. Прямые requirements закреплены проектом, транзитивного lock пока нет:
это воспроизводимый source, но не восстановление старого production image.

Отдельный previous `seed` subprocess создаёт минимальную upgrade-БД старым
`SQLiteAppUserRepository` на исходной старой схеме и завершает работу.
После этого только `migrations/*.sql` одноразового previous export заменяются
точными candidate bytes. Старый `migrations/__init__.py`, migration runner,
repositories, DTO, mappers, tests и helpers остаются байт-в-байт previous.
Новые и удалённые SQL учитываются в manifest; посторонний файл/каталог в
migrations отклоняется. Новый behavior subprocess импортирует старые
Python/tests **после** подстановки; default `migrations_dir` уже указывает
на candidate SQL. Seed-процесс и behavior-процесс не разделяют imports.

[repository_compatibility_worker.py](../../scripts/repository_compatibility_worker.py)
стартует с `-I -B`, без `PYTHONPATH` и user site. Дочерний Python неизменного
vault lock test наследует previous venv/cwd и запреты user site/записи pyc
через `PYTHONNOUSERSITE=1` и `PYTHONDONTWRITEBYTECODE=1`; origin manifest
worker не является трассировкой дочернего интерпретатора. До импорта проекта добавляются
только выбранный checkout и его `tests/`. Проверяются исходный `sys.path`,
отсутствие system site packages, origins project/tests/helpers до исполнения
и реальные `__file__` после tests. Проверяется неизменность `sys.path`.
После обоих worker повторно сверяются все файлы: в previous допустим только
заранее описанный SQL overlay, в candidate — ни одного изменения.
Это защита от случайного import leakage; запуск недоверенного Python
требует отдельного CI sandbox и не становится безопасным от этих проверок.

## Разделить результаты и ограничения

| Результат | Что проверяется в 5.2 | Что остаётся |
| --- | --- | --- |
| `candidate_migrations_data_integrity` | Candidate migrator: fresh, upgrade seed прежнего API, byte-exact префикс SQL, точный журнал, pending порядок, FK/integrity, идемпотентность и сохранность `id/display_name` одной строки; затем candidate `test_migration_runner` | Данные всех модулей, связи и двусторонний обмен из 5.3 |
| `old_repository_behavior` | 22 неизменных модуля из аудита 5.1 на SQL кандидата; profile наблюдает реальный old `apply_migrations`, сверяет журнал, FK/integrity и фиксирует fingerprint | Полное покрытие методов, per-test fixture attribution, postconditions после всех операций, дополнительные adapters и доверенная полнота manifest из 5.3/5.4 |

Профилирование не заменяет функцию мигратора, SQL запросов, результаты или
assertions. Наблюдение candidate-схемы обязательно для непустого behavior
набора, но **не доказывает**, что каждый test или каждая операция работали
на ней. IDs, failures/errors/skips и число observations сохранены для
следующих шагов; пропуск tests не выдаётся за успех. Test с custom-schema
или synthetic UNKNOWN остаётся отдельной областью аудита 5.1.

Fingerprint обозначает проверенную схему и не требует её совпадения с
previous. Если прежний migrator не понимает новый формат SQL, текущий
runner сообщает ошибку; отдельного fixture adapter для такого формата
в 5.2 нет. Старый [probe_previous_code.py](../../scripts/probe_previous_code.py)
остаётся частичным smoke действующего CI, его успех не заменяет old behavior.
Workflows, deploy, startup/admission и VPS этим шагом не изменяются.

## Проверить сам механизм

```powershell
& 'C:/Users/compadre/Documents/obsChatBot/.venv/Scripts/python.exe' -m pip check
& 'C:/Users/compadre/Documents/obsChatBot/.venv/Scripts/python.exe' -m unittest discover -s tests -p test_repository_compatibility_runner.py -v
```

На 02.11.2026: `pip check` успешен, 10 targeted tests успешны. Проверены
byte-exact Git export, отказ moving ref, неизменность previous Python и
`__init__.py`, отказ постороннего файла, guard чужих origins, реальный
subprocess import error, подстановка до вычисления default argument,
раздельные результаты при отказе candidate набора.
На одноразовом exact export реальные прежние identity tests успешно
и vault lease tests увидели добавленную synthetic SQL без изменения sources;
повтор с invalid SQL дал ошибки tests
и неуспешный worker. Эти мутации проверяют harness и не относятся к
admission реального кандидата.

Первый полный запуск выявил лишнее предположение об отсутствии tracked
`migrations/__init__.py`; runner отказал до old behavior. Исправление
сохраняет этот Python-файл previous и не переносит candidate `__init__.py`.
Ранний экспорт через `git archive` также был отклонён проверкой blob bytes;
окончательная реализация использует `cat-file`.

Финальный полный локальный прогон `data/step52-run4` завершился с exit code 0:
два новых venv установили собственные requirements и прошли `pip check`;
candidate выполнил fresh/upgrade проверки и 9 migration tests;
previous выполнил 147 behavior tests без errors/failures/skips.
Наблюдались 153 завершения старого `apply_migrations` с candidate journal.
Fingerprint схемы: `11e5bb7311a50c33e8cf5f7968c8b72ad54dfae0136dc02bbccba15367c3fd15`.
Финальные manifests не обнаружили ни одного изменения sources, а SHA256
runner/worker в отчёте совпали с оставленными для ревью файлами.
Это проверка механизма Windows/Python 3.12.10/SQLite 3.49.1, не доверенное
CI evidence и не утверждение полноты repository matrix.

Промежуточные прогоны правильно отказали из-за pyc, которые создавал
дочерний Python старого vault lock test. Запрет pyc перенесён также в
наследуемую среду; финальный прогон и targeted regression сохранили строгую
проверку всех файлов, не исключая `__pycache__` из manifest и не меняя tests.
