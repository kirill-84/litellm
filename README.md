# LiteLLM + Continue: tokens/sec с учётом отмен

Стек для сбора скорости генерации токенов (tokens/sec) **включая запросы, отменённые
пользователем** (Esc во время автодополнения в Continue), и их визуализации в Grafana.

Сервисы (`docker-compose.yml`): `litellm` (прокси, :4000) + `db` (Postgres) +
`continue-exporter` (приём телеметрии Continue, :4010) + `prometheus` (:9090) +
`grafana` (:3000).

## Запуск

1. Скопируйте `.env.example` → `.env` и заполните значения (master_key начинается с `sk-`,
   креды Postgres согласованы с `DATABASE_URL`).
2. В `config.yaml` замените `remote_ip` в `api_base` на адрес/IP машины с Ollama.
3. В `continue/config.yaml` укажите `apiKey`, **равный `LITELLM_MASTER_KEY`** из `.env`,
   и адрес деплой-машины в `apiBase`/`destination`.
4. Поднимите стек (из корня репозитория — все bind-mount файлы должны быть на месте):

```bash
docker compose up -d
```

Compose сам соблюдает порядок (`depends_on`): `db` → `litellm`, затем `prometheus`
(после `litellm` + `continue-exporter`) → `grafana`.

> `depends_on` гарантирует только порядок *старта*, а не готовность: `litellm` может
> стартовать раньше, чем Postgres начнёт принимать соединения (LiteLLM переживает это
> за счёт ретраев). Чтобы дождаться готовности по healthcheck'ам, запускайте с флагом
> `--wait`:
>
> ```bash
> docker compose up -d --wait
> ```

**Про сборку.** Образы `litellm`, `db`, `prometheus`, `grafana` — готовые (берутся из
реестра). Локально собирается только `continue-exporter` (`build: ./exporter`,
свой `Dockerfile`): на первом `up -d` Compose соберёт его автоматически. Но обычный
`docker compose up -d` **не пересобирает** уже собранный образ — после правок
`exporter/app.py` пересоберите его явно:

```bash
docker compose up -d --build continue-exporter   # или просто up -d --build целиком
```

> `--build` здесь безопасен: у `litellm` нет блока `build:` (только `image:`), поэтому
> флаг затрагивает лишь `continue-exporter`.

## Как считаются отменённые токены (важно)

- Continue логирует событие `tokensGenerated` **даже при отмене генерации** (с частично
  сгенерированными токенами). Поэтому суммарный `continue_generated_tokens_total`
  **уже включает** токены отменённых запросов — это и есть искомый cancel-inclusive
  tokens/sec. Серверные `litellm_*` при обрыве стрима, как правило, недосчитывают.
- **Выделить токены именно отмен — нельзя.** Событие `tokensGenerated` не содержит
  признака отмены и не связано с событием `autocomplete` (где есть `accepted`). Поэтому
  «cancel tokens/sec» как отдельной метрики не существует; доступна лишь прокси-доля
  отклонений по `accepted` (показано-но-не-принято), не взвешенная по токенам.

## Continue (на dev-машине)

`continue/config.yaml` шлёт телеметрию на экспортер. Ключевое: `level: noCode` (в журнал
не попадает исходный код), `apiKey` = `LITELLM_MASTER_KEY`.

## Метрики и запросы (Prometheus, :9090)

```promql
# tokens/sec, включая отменённые (клиентские данные Continue) — это и есть искомое число
sum(rate(continue_generated_tokens_total[1m]))

# серверная оценка LiteLLM для сравнения (НЕ складывать с continue_* — это тот же
# трафик через прокси, т.е. двойной счёт; при отмене litellm к тому же недосчитывает)
sum(rate(litellm_output_tokens_metric_total[1m]))

# прокси-доля отклонений автодополнения (НЕ доля отмен на лету)
continue_autocomplete_reject_rate_5m

# отклонённые автодополнения в секунду, отрисованные «в минус» (индикатор отказов;
# это ЗАПРОСЫ/сек, НЕ tokens/s — токенно-взвешенной отмены в dev-data нет)
0 - sum(rate(continue_autocomplete_total{accepted="false"}[5m]))
```

Готовые recording-правила — в `prometheus/rules/continue_litellm.yml`
(`*_per_sec_5m`, `continue_autocomplete_reject_rate_5m`, `continue_autocomplete_cache_hit_rate_5m`).

## Удаление кастомных метрик (PromQL-селектор)

Удалить накопленные данные кастомных `continue_*` метрик можно через **Prometheus
Admin API** (`POST /api/v1/admin/tsdb/delete_series`). Он принимает не полное
PromQL-выражение, а **селектор серий** (instant-vector selector — подмножество PromQL:
имя метрики + матчеры по лейблам, без функций вроде `rate()`). Admin API уже включён
флагом `--web.enable-admin-api` в `docker-compose.yml`.

```bash
# Удалить ВСЕ кастомные метрики Continue (-g отключает globbing curl для {} и [])
curl -g -X POST 'http://localhost:9090/api/v1/admin/tsdb/delete_series?match[]={__name__=~"continue_.*"}'

# Удалить одну метрику
curl -g -X POST 'http://localhost:9090/api/v1/admin/tsdb/delete_series?match[]=continue_autocomplete_total'

# Удалить только серии конкретной модели
curl -g -X POST 'http://localhost:9090/api/v1/admin/tsdb/delete_series?match[]={__name__=~"continue_.*",model="qwen2.5-coder-3b-instruct-q8_0"}'

# Удалить только за интервал времени (start/end — RFC3339 или unix-таймстемп)
curl -g -X POST 'http://localhost:9090/api/v1/admin/tsdb/delete_series?match[]={__name__=~"continue_.*"}&start=2026-05-01T00:00:00Z&end=2026-05-31T00:00:00Z'

# Сразу освободить место на диске (delete_series лишь ставит «надгробия»)
curl -X POST 'http://localhost:9090/api/v1/admin/tsdb/clean_tombstones'
```

Успешный вызов возвращает `204 No Content`.

> **Важно — удаление чистит только историю.** Счётчики `continue_*` живут в памяти
> экспортера и заново отдаются на каждом `/metrics`, поэтому при следующем scrape
> Prometheus снова создаст серии (с текущими значениями). Чтобы метрика реально
> перестала появляться:
> - временно — уберите её из `exporter/app.py` или остановите `continue-exporter`;
> - полностью обнулить накопленное — пересоздать экспортер вместе с томом, иначе
>   реплей журнала восстановит счётчики:
>   `docker compose rm -sf continue-exporter && docker volume rm <project>_continue_exporter_data && docker compose up -d continue-exporter`.

## Grafana (:3000, admin/admin)

Datasource Prometheus и дашборд **«LiteLLM + Continue — tokens/sec (cancel-inclusive)»**
поднимаются автоматически (provisioning в `grafana/`). Панели сгруппированы по строкам:

- **Пропускная способность** — generated/prompt tokens/sec, events/sec и общий счётчик;
  график «Continue (cancel-inclusive) vs LiteLLM (server-side)» отдельными рядами.
- **По моделям и LiteLLM** — generated tokens/sec в разбивке по моделям; LiteLLM
  input vs output.
- **Автодополнение и отмены (Esc)** — accepted vs rejected, reject-rate, cache-hit
  rate и cache hit/miss (кэш/прогрев объясняют, почему повторная отмена даёт «Complete»),
  а также **«Rejected autocompletions/sec (рендер в минус)»** — отклонённые
  автодополнения, отрисованные ниже нуля как индикатор отказов. Это **запросы/сек, НЕ
  tokens/s**: dev-data Continue не несёт токенов отмены, а `accepted=false` означает
  «показано-но-не-принято», что не равно «отменено на лету».
- **Здоровье пайплайна** — ingest errors/sec по причине, возраст последнего события,
  суммарные ошибки приёма.

## Ограничение

Точные частичные токены при жёсткой отмене существуют только если их прислал клиент.
Стек честно фиксирует то, что реально присылает Continue (`tokensGenerated`, в т.ч. для
оборванных генераций). Если Continue по какому-то событию пришлёт `generatedTokens=0` —
восстановить недостающее невозможно.
