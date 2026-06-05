# Замечания по графикам в Grafana (dashboards)

Путь: `@grafana/dashboards/tokens-per-sec.json`
Разбираем раздел - Метрики по пользователям и моделям из @README.md 

## Раздел - По пользователям и активности (кто чем пользуется)

**График:** `Кто чаще: autocomplete vs chat по пользователям (Continue)`

Параметр запроса: `$user` приходит пустым.
```
Metrics browser: sum by (user, event_name) (rate(continue_events_total{user=~"$user", event_name=~"autocomplete|chatInteraction|editOutcome"}[$__rate_interval]))
```
Но если смотреть график - `LiteLLM output tokens/sec по пользователям и моделям (server-side)`, в запросе:

```
sum by (end_user, model) (rate(litellm_output_tokens_metric_total[$__rate_interval]))
```
`end_user` - значение приходит.
Это из-за добавления отслеживания действий в `@config.yaml`. Справка: https://docs.litellm.ai/docs/tutorials/openweb_ui#32-tracking-usage--spend

```
general_settings:
  user_header_mappings:
    - header_name: X-OpenWebUI-User-Id
      litellm_user_role: internal_user
    - header_name: X-OpenWebUI-User-Email
      litellm_user_role: customer
```

Можно ли собрать статистику используя данные от уже приходящих запросов в litellm, там они более информативные, но как я думаю не покажут данные по признаку - кто чем больше пользуется. 
Может быть тогда лучше объединить от данных `continue_` и `litellm_` для построения графика?
Можно конечно добавить дополнительные пользовательские заголовки от Continue: https://docs.litellm.ai/docs/tutorials/openweb_ui#add-custom-headers-to-spend-tracking
