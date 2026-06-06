# LiteLLM + Continue: tokens/sec с учётом отмен

Стек для сбора скорости генерации токенов (tokens/sec) **включая запросы, отменённые пользователем** (Esc во время автодополнения в Continue), и их визуализации в Grafana.

Сервисы (`docker-compose.yml`): `litellm` (прокси, :4000) + `db` (Postgres) + `continue-exporter` (приём телеметрии Continue, :4010) + `prometheus` (:9090) + `grafana` (:3000).

## Запуск

1. Скопируйте `.env.example` → `.env` и заполните значения (master_key начинается с `sk-`, креды Postgres согласованы с `DATABASE_URL`).
2. В `config.yaml` замените `remote_ip` в `api_base` на адрес/IP машины с Ollama.
3. В `continue/config.yaml` укажите `apiKey`, **равный `LITELLM_MASTER_KEY`** из `.env`, и адрес деплой-машины в `apiBase`/`destination`.
4. Поднимите стек (из корня репозитория — все bind-mount файлы должны быть на месте):

```bash
docker compose up -d
```

Compose сам соблюдает порядок (`depends_on`): `db` → `litellm`, затем `prometheus` (после `litellm` + `continue-exporter`) → `grafana`.

> `depends_on` гарантирует только порядок *старта*, а не готовность: `litellm` может стартовать раньше, чем Postgres начнёт принимать соединения (LiteLLM переживает это за счёт ретраев). Чтобы дождаться готовности по healthcheck'ам, запускайте с флагом
> `--wait`:
>
> ```bash
> docker compose up -d --wait
> ```

**Про сборку.** Образы `litellm`, `db`, `prometheus`, `grafana` — готовые (берутся из реестра). Локально собирается только `continue-exporter` (`build: ./exporter`, свой `Dockerfile`): на первом `up -d` Compose соберёт его автоматически. Но обычный
`docker compose up -d` **не пересобирает** уже собранный образ — после правок `exporter/app.py` пересоберите его явно:

```bash
docker compose up -d --build continue-exporter   # или просто up -d --build целиком
```

> `--build` здесь безопасен: у `litellm` нет блока `build:` (только `image:`), поэтому флаг затрагивает лишь `continue-exporter`.

## Как считаются отменённые токены (важно)

- Continue логирует событие `tokensGenerated` **даже при отмене генерации** (с частично сгенерированными токенами). Поэтому суммарный `continue_generated_tokens_total` **уже включает** токены отменённых запросов — это и есть искомый cancel-inclusive tokens/sec. Серверные `litellm_*` при обрыве стрима, как правило, недосчитывают.
- **Выделить токены именно отмен — нельзя.** Событие `tokensGenerated` не содержит признака отмены и не связано с событием `autocomplete` (где есть `accepted`). Поэтому «cancel tokens/sec» как отдельной метрики не существует; доступна лишь прокси-доля отклонений по `accepted` (показано-но-не-принято), не взвешенная по токенам.

## Метрики по пользователям и моделям

Цель — видеть, **кто чем больше пользуется** (autocomplete vs chat) и **по каким моделям**.
Есть **два независимых источника идентичности**, и они отвечают на разные вопросы — это важно, потому что у каждого свой изъян:

| Вопрос | Источник | Метки | Изъян |
| --- | --- | --- | --- |
| Кто чаще **autocomplete или chat** | Continue (экспортер, client-side, cancel-inclusive) | `continue_events_total{user, event_name}` | тип активности виден **только тут** (LiteLLM его не различает), но `user`=Continue `userId` |
| Кто сколько **токенов** сжёг по моделям | LiteLLM (`/metrics`, server-side) | `litellm_*{end_user, user, model}` | авторитетный per-user, но **НЕ** cancel-inclusive |

- **Тип активности (autocomplete / chat / edit) различим только на стороне Continue** —
  через `event_name`. LiteLLM видит лишь модель. У вас модели разведены по ролям
  (Qwen2.5 → autocomplete, LLama3 → chat/edit), поэтому per-model на LiteLLM косвенно =
  per-role; но точный «кто чатился» даёт только Continue.
- **`user` в Continue = `userId`** — это идентичность Continue (Hub), а **не** пользователь
  OpenWebUI. На локальном VS Code без логина в Continue Hub `userId` **часто пустой** →
  метка схлопывается в `user="unknown"`.
- **`end_user`/`user` в LiteLLM** заполняются из `general_settings.user_header_mappings`:
  `X-OpenWebUI-User-Email` → роль `customer` → метка **`end_user`** (человекочитаемый email),
  `X-OpenWebUI-User-Id` → роль `internal_user` → метка **`user`**.
- **Метка `model` НЕ совпадает между источниками** (у Continue это имя модели Continue/
  OpenWebUI, у LiteLLM — `model_name` прокси). Группируйте в пределах одного источника.

> **⚠️ Каветы LiteLLM-пути (проверьте на живом стеке, прежде чем верить панелям):**
> - `user_header_mappings` по открытому багу [litellm#14667](https://github.com/BerriAI/litellm/issues/14667)
>   **может не мапиться с OpenWebUI** именно на `main-stable` (ваш образ) → метки
>   `end_user`/`user` будут пустыми. Фолбэк, который LiteLLM проверяет всегда (без
>   `user_header_mappings`): заставить OpenWebUI слать `x-litellm-end-user-id`.
> - end_user-учёт в Prometheus может требовать enterprise-лицензии (флаг
>   `enable_end_user_cost_tracking_prometheus_only` уже выставлен).

> **Масштабирование на любое число моделей.** Разрез по моделям — полностью label-based:
> метрики размечены меткой `model`, правила используют `sum by (model)`, а в дашборде
> переменная `$model` (`label_values(...)`) подхватывает любые модели **автоматически** —
> добавление моделей в `model_list` не требует правок кода или панелей (`continue/config.yaml`
> с Qwen/LLama — лишь пример). Единственное условие для **серверных** `litellm_*` по модели:
> её трафик должен идти через этот LiteLLM (т.е. модель присутствует в `model_list`). Модель,
> которую Continue зовёт мимо прокси, в `litellm_*` не появится — её видно только client-side
> (события Continue, в т.ч. `chatInteraction`). Имена моделей у Continue/OpenWebUI и LiteLLM
> могут не совпадать → группируйте в пределах одного источника.

> **Headline-проверка (на деплой-машине; решает, будут ли реальные имена вместо `unknown`):**
> ```bash
> # 1) Заполнен ли Continue userId? (если везде "" — per-user из экспортера будет unknown)
> docker exec continue-exporter \
>   sh -c 'grep -o "\"userId\":[^,}]*" /data/events.jsonl | sort | uniq -c'
> # 2) Заполнены ли метки LiteLLM end_user/user? (если пусто — это #14667 / лицензия)
> curl -s localhost:4000/metrics | grep 'litellm_output_tokens_metric_total' | grep -E 'end_user="[^"]+"|user="[^"]+"'
> # 3) Реально ли прилетает chatInteraction (иначе «chat»-половина панелей будет пустой)?
> docker exec continue-exporter \
>   sh -c 'grep -o "\"name\":\"[^\"]*\"" /data/events.jsonl | sort | uniq -c'
> # 4) Есть ли у autocomplete реальная метка model? (пусто ⇒ панели 28/30 «accept/reject
> #    по модели» схлопнутся в одну строку model="unknown")
> curl -s localhost:4010/metrics | grep continue_autocomplete_total | grep -v 'model="unknown"'
> ```
> Риски **связаны**: если `userId` пуст **и** сработал #14667 — per-user данных нет
> нигде; тогда чините хотя бы один путь.

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

# «скорость на запрос», как в Continue Console: completion ÷ duration (457/50.361 ≈ 9.07).
# Взвешенное среднее Σ(output tokens) ÷ Σ(request latency). Источник — LiteLLM, поэтому
# НЕ cancel-inclusive; требует гистограммы litellm_request_total_latency_metric на /metrics.
# Это ИНОЕ число, чем sum(rate(...)) выше (тот делит на стену времени, включая простои).
sum(rate(litellm_output_tokens_metric_total[5m]))
  / sum(rate(litellm_request_total_latency_metric_sum[5m]))

# та же «скорость на запрос», но ПО МОДЕЛЯМ (видно, какая модель генерирует быстрее).
# Деление litellm-метрики на litellm-метрику — метка model у обеих одинаковая, сопоставление
# точное (нет рассогласования имён, как на мосту Continue↔litellm).
sum by (model) (rate(litellm_output_tokens_metric_total[5m]))
  / sum by (model) (rate(litellm_request_total_latency_metric_sum[5m]))

# --- По пользователям и моделям (подробности и каветы — в разделе ниже) ---
# «Кто чаще autocomplete или chat» (Continue, client-side): тип активности есть ТОЛЬКО тут,
# через event_name. Метка user = Continue userId (часто пуста на локальном VS Code).
sum by (user, event_name) (rate(continue_events_total{event_name=~"autocomplete|chatInteraction|editOutcome"}[5m]))

# tokens/sec (cancel-inclusive) по пользователям и по моделям
sum by (user)  (rate(continue_generated_tokens_total[5m]))
sum by (model) (rate(continue_generated_tokens_total[5m]))

# авторитетный per-user учёт LiteLLM через user_header_mappings (server-side, НЕ cancel-inclusive):
# end_user = X-OpenWebUI-User-Email, user = X-OpenWebUI-User-Id. Если ряды пусты — см. каветы ниже.
sum by (end_user, model) (rate(litellm_output_tokens_metric_total[5m]))

# прокси-доля отклонений автодополнения (НЕ доля отмен на лету)
continue_autocomplete_reject_rate_5m

# accept/reject ПО МОДЕЛИ (user часто пуст → разрез по модели информативнее).
# Кладётся рядом с per-user токенами litellm: слева «какая модель чаще принимается»,
# справа «кто её потребляет». accepted есть ТОЛЬКО в Continue — связать с litellm по
# пользователю нельзя (общий ключ лишь model, он грубее пользователя).
sum by (model, accepted) (rate(continue_autocomplete_total[5m]))
sum by (model) (rate(continue_autocomplete_total{accepted="false"}[5m]))
  / sum by (model) (rate(continue_autocomplete_total[5m]))

# отклонённые автодополнения в секунду, отрисованные «в минус» (индикатор отказов;
# это ЗАПРОСЫ/сек, НЕ tokens/s — токенно-взвешенной отмены в dev-data нет)
0 - sum(rate(continue_autocomplete_total{accepted="false"}[5m]))
```

Готовые recording-правила — в `prometheus/rules/continue_litellm.yml`
(`*_per_sec_5m`, `continue_autocomplete_reject_rate_5m`, `continue_autocomplete_cache_hit_rate_5m`,
а также разрезы по пользователям/моделям: `continue_events_per_sec_by_user_event_5m`,
`continue_generated_tokens_per_sec_by_user_5m`/`_by_model_5m`,
`continue_autocomplete_per_sec_by_model_accepted_5m`, `continue_autocomplete_reject_rate_by_model_5m`,
`litellm_output_tokens_per_sec_by_end_user_model_5m`/`_by_user_model_5m`).

> **Про окна `rate()`.** Примеры выше — с фиксированными окнами (`[1m]`/`[5m]`), потому
> что их запускают прямо в Prometheus (:9090). На **дашборде Grafana** окна `rate()`
> заданы как `$__rate_interval` — Grafana авто-подбирает ширину под scrape-интервал и
> зум (≈ `max(4×scrape, шаг+scrape)`), вместо жёстких 5m. Это **переменная Grafana**: в
> самом Prometheus и в recording-правилах её нет, поэтому правила остаются на 5m. Side
> effect: при редком трафике короткие окна дают больше провалов `0/0`, чем `[5m]`.

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
  график «Continue (cancel-inclusive) vs LiteLLM (server-side)» отдельными рядами; и
  **«Tokens/sec на запрос — completion ÷ duration»** — Console-метрика скорости одного
  запроса (`457/50.361 ≈ 9.07`), считается из гистограммы латентности LiteLLM как
  взвешенное среднее. ⚠️ Это **НЕ cancel-inclusive** (источник — LiteLLM, при жёсткой
  отмене недосчитывает) и **не равно** агрегатному `sum(rate(...))`, который делит на
  стену времени с простоями (тот же запрос там ≈ `457/300 ≈ 1.5`). Второй ряд — decode
  speed без TTFT (валиден только для стриминга). Ниже отдельной панелью — **та же скорость
  ПО МОДЕЛЯМ** (`… by (model) …`): видно, какая модель генерирует быстрее; деление
  litellm/litellm, метка `model` совпадает → сопоставление точное.
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
- **По пользователям и активности** — отвечает на «кто чем больше пользуется»:
  *autocomplete vs chat по пользователям* (`continue_events_total` by `user, event_name` —
  тип активности есть только client-side), *generated tokens/sec по пользователям*
  (cancel-inclusive), *LiteLLM output tokens/sec по `end_user, model`* (авторитетный
  per-user, но НЕ cancel-inclusive — ⚠️ при пустых рядах см. #14667/лицензию выше) и
  *таблица-лидерборд* событий по пользователю и типу за период. Вверху дашборда —
  переменные **`$user`** и **`$model`** (мультивыбор) для фильтрации рядов Continue;
  при пустом Continue `userId` пользователь будет один — `unknown`.
- **Accept/reject по модели ↔ потребление по пользователям** — сопоставление через метку
  `model`: слева *accepted vs rejected по модели* (Continue — единственный источник
  принятия), справа *LiteLLM output tokens/sec по `end_user, model`* (кто реально
  потребляет модель), снизу во всю ширину — *reject-rate по модели*. ⚠️ Это
  **сопоставление по модели, а не join по пользователю**: per-user accept/reject
  недостижим — принятие живёт только в Continue (где `userId` пуст), а identity по людям
  приходит лишь из LiteLLM (`end_user`), и общего ключа тоньше модели между источниками
  нет. Два рассогласования, которые надо держать в голове: (1) значения метки `model` у
  Continue и LiteLLM могут различаться (сопоставляйте по смыслу); (2) **наборы моделей
  разные** — слева только модели роли autocomplete (`continue_autocomplete_total` пишется
  лишь для них), справа — все модели, включая chat; chat-модели справа левого аналога не
  имеют.

> Окна `rate()` на всех панелях — динамические (`$__rate_interval`, не фиксированные 5m).
> Панели reject-rate / cache-hit раньше брались из recording-правил `*_5m`; теперь их
> выражения вынесены инлайн с `$__rate_interval`, поэтому правила в Prometheus остаются
> (для ad-hoc-PromQL), но дашбордом не используются.

## Ограничение

Точные частичные токены при жёсткой отмене существуют только если их прислал клиент.
Стек честно фиксирует то, что реально присылает Continue (`tokensGenerated`, в т.ч. для
оборванных генераций). Если Continue по какому-то событию пришлёт `generatedTokens=0` —
восстановить недостающее невозможно.
