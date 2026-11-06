# Шаг 7.7: закрыть публичный лимит в Release selector

Этот протокол фиксирует исправление read-only допуска после
[отказа recovery preflight](STAGE7_7_DEPLOY_INCIDENT.md#дополнительный-отказ-допуска-после-установки-snapshot).
Установленный controller и production state этим действием не менялись;
порядок дальнейшего восстановления — в [операционной инструкции](../OPERATIONS.md#отказ-выпуска-и-возврат).

## Исправить выбор GitHub API

06.11.2026 `Host.select()` при наличии root-private `github-read-token`
передавал в `GitHub` пустой token и `allow_anonymous=True`. Повторный
production admission получил HTTP 403 `rate limit exceeded` от публичного
endpoint `/actions/workflows/main-full.yml`. Отдельный вызов того же endpoint
с установленным token прошёл; значение token и HTTP body не выводились.

Новый `Host.release_api()` для обычного и аварийного selector использует
private token при наличии файла. `lexists` не пропускает dangling symlink в
анонимный fallback; `private_text` требует root:root/0600 и непустое значение.
Если token отсутствует при наличии v2 state или selector, допуск закрывается.
Анонимный путь остаётся только для старого legacy состояния без token.
Существующие проверки Release, tag, exact CI run/attempt, artifact и
previous SHA не ослаблены.

Через проектный venv выполнены 96 целевых `unittest` для release selection,
evidence, controller snapshot и v2 recovery: **OK**, один Linux-only skip.
Тесты проверяют передачу private token в оба selector и повторные чтения,
синтетический публичный 403 при успешных двух authenticated чтениях, а также
отказ при malformed token и v2 без token. PR
[#75](https://github.com/karateka-git/compadre-assistant/pull/75) прошёл
`pr-fast` и слит merge commit
`aeeb73c557face466ce411b8c22a4aad04750861`.

## Подтвердить exact snapshot на Linux

Автоматический полный [main-full 42284495337/1](https://github.com/karateka-git/compadre-assistant/actions/runs/42284495337)
завершился **success** для exact merge SHA: controller,
`repository-compatibility`, `full-suite`, `ci-metrics` успешны. Reviewed CI
policy variable оставался `09f15a5b3c01ec73d02379b918104e8cf697b4b2`
для установленной до инцидента базы v3.1.0; production external pin для
пары v3.1.0 → v3.2.0 не менялся.

Из exact Git SHA собран archive SHA256
`b193a91a7ed40c571b951f2832edbd7f7b4f8a1e780d643769c4a791dbfa091c`,
64 файла. На VPS архив размещён **только** в закрытом каталоге
`/var/lib/compadre-assistant-stage7-20261106/controller-aeeb73c-proof/archive.tar.gz`.
Linux `unpack` и `verify --require-root` подтвердили archive/manifest, права
root и isolated imports private staging. Из private staging два вызова
`Host.select()` с подменённым только parser Release успешно выполнили реальный
authenticated `GitHub.get()` к Actions endpoint; полный selection и exact-pair
проверятся отдельно в production admission. Значение token не выводилось.

На момент проверки production установленный controller SHA оставался
`38be03d75293cbf516456fe45ead9bc17fedcc8d`; state `degraded`, marker
`critical`, timer выключен, оба candidate канала остановлены. Следующий шаг —
отдельная установка нового controller snapshot под существующим deploy.lock,
проверка exact units и отсутствие изменений БД/state, затем новый fresh
admission перед единственной командой forward recovery. Обычный `deploy-prod`
не повторять.
