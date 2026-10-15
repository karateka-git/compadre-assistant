# Этап 5, шаг 5: готовность к первому выпуску

Снимок только для чтения: 2026-10-15, 08:35 UTC. Первый выпуск на VPS **не
выполнен**. Факты ниже нужно проверить заново непосредственно перед установкой
контроллера и переключением. Порядок приёмки относительно слияния рабочей
ветки остаётся открытым решением; этот документ его не устанавливает.

## GitHub и рабочая ветка

- `main` указывает на `73b0a4e58b8b654e0593ba634ca71b94264f2bd1`.
  `.github/workflows/main-full.yml` в `main` отсутствует; зарегистрированы
  только `PR fast` и `Release full`.
- PR [#9](https://github.com/karateka-git/compadre-assistant/pull/9) открыт как
  draft, его опубликованный head — `4f8a5a4ccf01c87c6074fbd9b883ce473ec8016e`.
  `pr-fast` этого head успешен. Локальный checkout рабочей ветки на
  `b9b13a42587c314c3987d6c8947ae62cad09df6e`, на четыре commit впереди
  опубликованной ветки; этот локальный SHA в GitHub CI ещё не проверялся.
- Настоящей ветки `release/1.0`, тегов `v*` и опубликованных GitHub Releases
  нет. Пробный `release-full` на временной линии `release/0.0` не допускает
  обычный выпуск.

## VPS

- `/opt/obs-chat-bot`: чистый detached checkout
  `ac2ac981ba628a881b483238a4eaf191a644de6a`.
- `tg_assistant` и `vk_assistant` работают и имеют `healthy`, `restarts=0`,
  `restart policy=unless-stopped` и один фактический image ID
  `sha256:1b7521619423d8607ec449d3dcabdac009334d8833d6f71874c670f2d55f0743`.
  Последние 300 строк журналов каждого канала содержат по одному сигналу
  запуска polling и не содержат `Traceback`; это не проверка реальных ответов.
- `obs-chat-bot.service` и `obs-chat-bot-backup.timer` активны и включены;
  `NeedDaemonReload=no`. Следующее срабатывание timer при проверке —
  2026-10-16 03:15 UTC.
- Production SQLite и ночная копия `app-20261015T031535Z.db` проходят
  `PRAGMA integrity_check`; в обеих только `0001_initial_schema.sql`.
  Копия имеет mode `0600` в каталоге `0700`.
- Свободно 7,2 GB. GitHub API отвечает HTTP 200, серверный read-only deploy
  key читает `main`, root читает checkout. Docker 29.8.0, Compose v5.5.1 и
  Python `sqlite3` доступны. CLI `sqlite3` не установлен.
- `/usr/local/lib/compadre-assistant-deploy`, `/usr/local/sbin/deploy-prod`
  и `/var/lib/compadre-assistant-deploy` отсутствуют; pin, drop-ins и state
  ещё не установлены.

### Права текущей SQLite

`/opt/obs-chat-bot/data/app.db` имеет mode `0644`, каталог `data` — `0755`.
Это допускает чтение БД другими локальными пользователями при доступе к
пути. Перед выпуском отдельно оценить и согласовать ограничение прав, затем
проверить доступ работающих контейнеров и ночного backup. Read-only аудит
права не менял. `.env` и PEM имеют mode `0600`; их содержимое не читалось.

## Условия допуска и проверка после будущего merge

1. Принять решение о порядке приёмки; опубликовать все согласованные изменения
   PR #9 с отдельным разрешением на push, снять draft и слить после успешного
   `pr-fast` точного опубликованного head.
2. Получить успешный **полный** `main-full` для выбранного SHA `main`, включая
   обязательный reusable `full-suite`. Первый merge, который добавит caller,
   является точкой отсчёта счётчика PR; до трёх следующих PR полный набор
   нужно запросить отдельно перед релизом. Успех одного controller job
   допуском не является.
3. От этого SHA создать `release/1.0`, проверить повторное использование
   полного CI, опубликовать защищённый tag и GitHub Release `v1.0.0` только
   после отдельного решения о публикации. Повторно сверить точные SHA, run ID
   и attempt перед командой на VPS.
4. Получить отдельное эксплуатационное разрешение на установку, контролируемый
   тест старого pin, reboot и затем реальный выпуск. Выполнить порядок из
   [STAGE5_STEP4.md](STAGE5_STEP4.md): инвентаризация, установка проверенного
   snapshot, `deploy-prod --verify-pinned`, проверка после reboot,
   `deploy-prod v1.0.0`, сверка SQLite, двух контейнеров, systemd и timer.
   Реальные ответы Telegram и VK подтверждает владелец до приёмки выпуска.

Read-only проверка точного `main` после merge из рабочего worktree в
PowerShell. `--check-only` не создаёт релизную ветку и проверяет обязательные
jobs и steps полного CI; если полного run нет, публикация остаётся закрытой.

```powershell
git fetch origin main
$sha = (git rev-parse origin/main).Trim()
gh run list --repo karateka-git/compadre-assistant --workflow main-full.yml --commit $sha --json databaseId,attempt,headSha,status,conclusion
$previousToken = $env:GITHUB_TOKEN
$env:GITHUB_TOKEN = gh auth token
try {
    python -m scripts.create_release_branch release/1.0 $sha --check-only
} finally {
    if ($null -eq $previousToken) {
        Remove-Item Env:GITHUB_TOKEN -ErrorAction SilentlyContinue
    } else {
        $env:GITHUB_TOKEN = $previousToken
    }
}
```

Проверка из этого блока не заменяет повторную инвентаризацию VPS и решение
о публикации. Пока нет опубликованного допустимого Release и разрешения на
VPS, не устанавливать controller, не переключать каналы и не отмечать шаг 5.5
завершённым.
