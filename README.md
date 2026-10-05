# Запуск обоих сборщиков в облаке

Актуальная настройка GitHub Actions + cron-job.org описана в [CLOUD.md](CLOUD.md).
Общий workflow: `Flights and theater collector`. Старый почасовой workflow ниже
оставляйте отключённым, чтобы не дублировать сбор.

# Vivaticket reconnaissance

A small Python/Playwright script to observe where ticket data comes from. Chromium is visible by default. It monitors the whole browser context, including frames and new tabs, without automatically reserving or buying tickets.

## Setup and run

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
python scraper.py
```

The browser stays open for 45 seconds after navigation. You can dismiss consent notices and inspect ticket details while recording. Use `python scraper.py --wait 120` for more time, or `--headless` to hide the browser. `--url 'https://…'` investigates another page.

Each run creates its own timestamped directories:

- `output/<run>/network.har`: full browser HTTP traffic with response bodies embedded.
- `output/<run>/network.json`: response metadata and failed requests; XHR/fetch records also include request bodies.
- `output/<run>/rendered.html`: final rendered page (additional tabs get separate HTML files).
- `output/<run>/document-*.html`: original HTML responses, useful for distinguishing server-rendered data from later DOM changes.
- `output/<run>/page.png`: final page screenshot.
- `output/<run>/run.json`: run settings and any navigation error.
- `responses/<run>/*.json`: parsed JSON response bodies, linked from `network.json`.
- `responses/<run>/*.xml`: XML bodies, including Vivaticket's observed ticket and room-layout API responses.

All XHR/fetch requests and responses are logged, including HTML responses. Ticket-related URL keywords receive additional priority in logging. JSON is detected using both content type and parsing text/XHR responses. The HAR retains other body formats, such as HTML, XML, and JavaScript, for inspection.

Let the recording finish so the browser context closes and flushes the HAR. Captures can contain cookies, session tokens, and personal data; keep them private. A consent screen, expired performance, or failed request can limit what a particular run reveals. `scraper.py` gathers reconnaissance evidence; `sync.py` extracts structured snapshots and uploads them to Supabase as described below.

## Сбор актуальных данных и отправка в Supabase

`sync.py` открывает страницу заново, извлекает свежий XML из сетевых ответов, сохраняет понятный `snapshot.json` и отправляет его в таблицу `ticket_snapshots`. Каждый запуск сохраняет отдельный снимок для истории цен и доступности. Суммы хранятся целыми числами в центах: `4853` означает €48,53. Тарифы абонементов помечены `is_subscription`, чтобы нулевая стоимость не выглядела бесплатным обычным билетом.

### Подключение

1. Создай проект в Supabase, если пока зарегистрирован только аккаунт.
2. Открой SQL Editor своего проекта и выполни содержимое `supabase.sql` один раз.
3. Скопируй `.env.example` в `.env`:

   ```sh
   cp .env.example .env
   .venv/bin/python -m pip install -r requirements.txt
   ```

4. В `.env` укажи Project URL и Secret key из настроек проекта:

   ```dotenv
   SUPABASE_URL=https://YOUR_PROJECT_REF.supabase.co
   SUPABASE_SECRET_KEY=sb_secret_YOUR_KEY
   ```

Ключ остаётся на твоём компьютере; `.env` исключён из Git. Используй Secret key, а не Publishable key. Новый Secret key отправляется в заголовке `apikey`; старый JWT service_role тоже поддерживается через переменную `SUPABASE_SERVICE_ROLE_KEY`. Таблица закрыта для `anon` и `authenticated`, запись выполняется локальным скриптом с серверным ключом. См. [документацию Supabase о ключах](https://supabase.com/docs/guides/getting-started/api-keys).

### Запуск

```sh
# Свежие данные и отправка в Supabase; браузер виден.
.venv/bin/python sync.py

# Сбор без отправки и без настройки Supabase.
.venv/bin/python sync.py --dry-run

# Дать странице больше времени или скрыть браузер.
.venv/bin/python sync.py --wait 60
.venv/bin/python sync.py --headless

# Другой спектакль: URL должен содержать pcode и tcode.
.venv/bin/python sync.py --url 'https://teatrodiroma.vivaticket.it/index.php?nvpg[sell]&cmd=prices&tcode=tl016248&pcode=14459254'

# Повторить отправку сохранённого снимка после ошибки подключения.
.venv/bin/python sync.py --upload-file output/RUN_ID/snapshot.json
```

Успешная отправка подтверждается строкой `Supabase confirmed upload`. Если свежий XML не получен, скрипт завершится с ошибкой и ничего не отправит. При ошибке отправки `snapshot.json` остаётся локально. Повторная отправка одного файла обновляет ту же запись по UUID, без дублей; новый сбор создаёт новую запись истории.

В Table Editor открой `ticket_snapshots`. В каждой строке будут название, театр, время спектакля, время наблюдения, общее количество доступных мест и `zones` со всеми секторами и тарифами. SQL-запрос для последних снимков находится в конце `supabase.sql`. Скрипт работает однократно; для нового наблюдения запусти его снова. Он собирает выбранный спектакль, не обходит весь каталог.

Проверки извлечения цен и отправки REST-запросов:

```sh
.venv/bin/python -m unittest -v test_sync
```

## Каждый час через GitHub Actions

Workflow `.github/workflows/vivaticket-sync.yml` запускает сбор каждый час на 17-й минуте и позволяет запускать его вручную. Chromium работает в headless-режиме; компьютер можно выключать. Каждый запуск собирает выбранный спектакль и отправляет новый снимок в существующую таблицу Supabase.

1. Загрузи проект в GitHub-репозиторий. Workflow должен находиться в основной ветке репозитория (обычно `main`). Загружай исходники, `requirements.txt`, `test_sync.py`, `.gitignore` и папку `.github`; `.env`, `.venv`, `output` и `responses` остаются локальными.
2. В репозитории открой **Settings → Secrets and variables → Actions → New repository secret**. Создай два секрета:
   - `SUPABASE_URL` — значение из локального `.env`.
   - `SUPABASE_SECRET_KEY` — значение из локального `.env`.
3. Открой **Actions → Vivaticket hourly sync → Run workflow** для первого проверочного запуска.
4. Проверь шаг **Collect fresh ticket data and upload to Supabase**: успех подтверждает строка `Supabase confirmed upload`. Новая запись появится в `ticket_snapshots`.

Не создавай `.env` в репозитории: workflow передаёт секреты через переменные окружения. Расписание использует cron `17 * * * *`, то есть `00:17`, `01:17` и далее в UTC. На часовом интервале переход между летним и зимним временем не меняет частоту. GitHub может задерживать или пропускать запуски при высокой нагрузке, поэтому точные интервалы не гарантированы. См. [документацию расписаний GitHub](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

Workflow устанавливает Chromium вместе с системными зависимостями, как описано в [Playwright CI](https://playwright.dev/python/docs/ci). Одновременные запуски ограничены, время выполнения ограничено десятью минутами. `snapshot.json` и `run.json` сохраняются в Artifacts на три дня; полные HAR и браузерные ответы в Artifacts не загружаются.

Расписание продолжает работать, пока workflow включён. Для сбора в течение трёх недель после 21 дня открой **Actions → Vivaticket hourly sync → меню ⋯ → Disable workflow**. При идеальном расписании получится 504 снимка на спектакль.

Первый запуск на GitHub также проверяет доступность Vivaticket с IP облачного runner. Успешный локальный запуск не гарантирует, что сайт пропустит облачный Chromium. Если сайт показывает проверку или не отдаёт XML, workflow завершится с ошибкой и не отправит пустой снимок.
