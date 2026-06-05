# Замечания по графикам в Grafana (dashboards)

Путь: `@grafana/dashboards/tokens-per-sec.json`\
Разбираем раздел - Метрики по пользователям и моделям из @README.md

Раздел - Автодополнение и отмены (Esc) оставляем, но хочется получать более подробную статистику от всех пользователях {accepted="true"} и {accepted="false"}, возможно совмещать статистику от 'litellm'.

И проверь еще раз пожалуйста вопрос со скоростью генерации токенов в сек.\
В `Continue` же приходят данные `tokensGenerated` и `Total Time`. По формуле же можно понять скорость генерации токенов в сек: `tokensGenerated` / `Total Time`.\
Или `tokensGenerated` это уже и есть скорость генерации токенов в сек?

Или брать скорость генерации токенов в сек. через метрики и Metadata в `litellm`?

**Metrics**
```
Duration: 0.227 s
```

**Metadata**
```
{
  "status": null,
  "max_retries": 2,
  "batch_models": null,
  "usage_object": {
    "total_tokens": 211,
    "prompt_tokens": 209,
    "completion_tokens": 2,
    ...
  }
...
}
```
Тут нужен - `completion_tokens`. Получается формула: `2 / 0.227 = 8,81 tokens/s`\
Поравь меня если я не прав.

## Раздел - По пользователям и активности (кто чем пользуется)

**График:** `Кто чаще: autocomplete vs chat по пользователям (Continue)`

Параметр запроса: `$user` - приходит пустым.
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

Можно ли собрать метрику используя данные от уже приходящих запросов в `litellm`, там они более информативные, но как я думаю не покажут данные по признаку - кто чем больше пользуется (auotcomplete или чат, тут только от Continue). Возможно я ошибаюсь.

**Request**
```json
{
  model: "Qwen2.5-Coder-32B-Instruct",
  ...
  metadata {
    headers: {
      x-openwebui-user-role:"user",
      x-openwebui-user-email:"user@mail.com"
    }
  }
}
```

Посмотри, может быть тогда лучше объединить данные `continue_` и `litellm_` для построения графиков?\
Возможно конечно нужны дополнительные пользовательские заголовки от Continue: https://docs.litellm.ai/docs/tutorials/openweb_ui#add-custom-headers-to-spend-tracking
Есть еще приходящие - `Metadata`
