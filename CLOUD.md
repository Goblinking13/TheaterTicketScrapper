# Авиабилеты и театр через GitHub Actions и cron-job.org

Workflow `Flights and theater collector` (`.github/workflows/flightwatch.yml`)
запускается только по `workflow_dispatch`: встроенного расписания GitHub нет.
cron-job.org отправляет POST каждый час на 17-й минуте UTC. Театр проверяется
один раз за текущий час, авиабилеты — один раз за текущий 12-часовой слот
21-дневной кампании. Пропущенные исторические цены не восстанавливаются.

Секреты GitHub Actions: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`,
`THEATER_SUPABASE_URL`, `THEATER_SUPABASE_KEY`.
Пустые театральные настройки используют основной проект Supabase.
Переменная `CAMPAIGN_START` должна содержать прежнюю постоянную дату начала
кампании. Не меняйте её при переносе. `PROVIDER` по умолчанию — `ryanair`.

Перед переключением сохраните `ledger.json`, `local-start.json` и
`theater/latest.json` из рабочего локального `state/` в GitHub Secret
`INITIAL_COLLECTOR_STATE`: JSON-объект, ключи которого — эти относительные
пути, значения — разобранное JSON-содержимое файлов. Секрет применяется только
при первом запуске без предыдущих completed runs. Непустые pending-очереди
сначала доставьте локально; скрипт начального переноса не импортирует снимки.

Каждый запуск восстанавливает полный `state/` из последнего artifact и сохраняет
новый, включая неотправленные снимки. Хранятся последние два checkpoint.
Одновременные запуски сериализованы; сбой одного сборщика не останавливает другой.
Если после предыдущих запусков checkpoint отсутствует, сбор прекращается:
восстановите `state/` вручную вместо сброса кампании. Полные HAR не сохраняются.
Срок artifacts — 90 дней; при длительном отключении сохраните резервную копию.

## Настройка внешнего триггера

Создайте GitHub fine-grained PAT, ограниченный этим репозиторием, с правом
`Actions: Read and write`. cron-job.org хранит этот токен и использует его
для авторизованных запросов. До истечения PAT замените его в задании.
API-ключ cron-job.org получите в Settings → API. Оба ключа сохраните в локальных
файлах вне git с правами 600. Установите Python-зависимости и выполните:

```sh
python scripts/configure-cron.py \
  --repo Goblinking13/TheaterTicketScrapper \
  --cron-key-file /path/to/cron-job-api-key.txt \
  --github-token-file /path/to/github-dispatch-token.txt
```

По умолчанию задание отключено. Повтор команды обновляет то же задание.
Сначала выполните **Actions → Flights and theater collector → Run workflow**,
mode=`verify` (свежий тест VIE и театра). Проверьте журнал, artifact и запись Supabase. Затем остановите
локальный LaunchAgent и повторите команду с `--enable`.
Старый workflow `Vivaticket hourly sync` должен оставаться отключённым.

Ручная настройка cron-job.org:

- URL: `https://api.github.com/repos/Goblinking13/TheaterTicketScrapper/actions/workflows/flightwatch.yml/dispatches`
- Метод: POST; расписание: каждый час, минута 17, timezone UTC.
- Headers: `Authorization: Bearer YOUR_PAT`, `Accept: application/vnd.github+json`,
  `Content-Type: application/json`, `X-GitHub-Api-Version: 2022-11-28`.
- Body: `{"ref":"main","inputs":{"mode":"tick"}}`.

Успешный HTTP-ответ означает принятие запуска GitHub. Результат самого сбора
проверяйте в Actions: cron-job.org не ждёт завершения workflow.
Доступ сайтов с IP облачного runner проверяется первым реальным сбором.

Документация: [workflow dispatch](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event),
[cron-job.org API](https://docs.cron-job.org/rest-api.html).

## Проверенный перенос 5 октября 2026

Дата начала кампании сохранена: `2026-10-04T20:54:58.719948+00:00`.
[Проверочный облачный запуск](https://github.com/Goblinking13/TheaterTicketScrapper/actions/runs/37302333641)
получил 2 предложения VIE на 18 февраля 2027 и театральный снимок с 302
доступными местами; загрузка в Supabase и сохранение checkpoint успешны.
Задание cron-job.org: `8583162`. Локальная служба `com.flightwatch.local`
остановлена, автозапуск отключён через `launchctl disable`.

Для обратного переключения сначала отключите задание cron-job.org, доставьте
облачные pending-снимки и перенесите последний checkpoint в рабочий локальный
`state/`. Затем включите и загрузите службу:

```sh
launchctl enable "gui/$(id -u)/com.flightwatch.local"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.flightwatch.local.plist"
```
