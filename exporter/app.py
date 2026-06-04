from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, generate_latest

PORT = int(os.getenv("PORT", "4010"))
JOURNAL_PATH = Path(os.getenv("JOURNAL_PATH", "/data/events.jsonl"))
STATE_PATH = Path(os.getenv("STATE_PATH", "/data/state.json"))

app = FastAPI(title="Continue Telemetry Exporter", version="2.1.0")
lock = threading.Lock()
seen_ids: set[str] = set()

registry = CollectorRegistry()

# Continue dev-data HTTP destination присылает КОНВЕРТ:
#   {"name": <eventName>, "data": {...реальные поля...}, "schema": ..., "level": ..., "profileId": ...}
# Поэтому тип события берём из top-level "name", а поля (model/provider/токены/accepted) — из "data".
#
# Про метку user: у КАЖДОГО события dev-data 0.2.0 в "data" есть базовое поле
# userId (см. base.ts схемы Continue). Это идентичность Continue (Hub), а НЕ
# пользователь OpenWebUI: при локальном VS Code без логина в Continue Hub userId
# часто пустой → метка станет "unknown". Авторитетный мульти-пользовательский
# учёт по людям живёт на стороне LiteLLM (метки end_user/user из user_header_mappings),
# а здесь user даёт разрез «кто чем пользуется» только если userId реально заполнен.
# Тип активности (autocomplete vs chat vs edit) виден ТОЛЬКО здесь — через event_name;
# LiteLLM его не различает (видит лишь модель). Поэтому «кто чаще autocomplete или chat»
# берётся из continue_events_total by (user, event_name) — отдельный счётчик чата не нужен.
#
# Про отмены (cancel): событие tokensGenerated НЕ несёт признака отмены и не имеет
# id для связи с событием autocomplete. При этом Continue (_logEnd) логирует
# tokensGenerated даже при abort, с частично сгенерированными токенами — то есть
# суммарный continue_generated_tokens_total УЖЕ включает отменённые запросы
# (cancel-inclusive). Выделить токены именно отмен из dev-data невозможно.
#
# Про разрез по моделям: tokensGenerated несёт model/provider, autocomplete —
# modelName/modelProvider, chatInteraction — modelName/modelProvider/modelTitle.
# Значения метки model МОГУТ не совпадать с метками model у litellm_* (разные
# идентификаторы у Continue/OpenWebUI/LiteLLM) — группируйте в пределах источника.

continue_events_total = Counter(
    "continue_events_total",
    "Total Continue telemetry events received. event_name распознаёт тип активности "
    "(autocomplete / chatInteraction / editOutcome / tokensGenerated / toolUsage); "
    "разрез by (user, event_name) отвечает на вопрос «кто чаще autocomplete или chat».",
    ["event_name", "user", "model", "provider"],
    registry=registry,
)
continue_prompt_tokens_total = Counter(
    "continue_prompt_tokens_total",
    "Prompt tokens reported by Continue (tokensGenerated events; cancel-inclusive).",
    ["user", "model", "provider"],
    registry=registry,
)
continue_generated_tokens_total = Counter(
    "continue_generated_tokens_total",
    "Generated tokens reported by Continue (tokensGenerated events; cancel-inclusive).",
    ["user", "model", "provider"],
    registry=registry,
)
continue_autocomplete_total = Counter(
    "continue_autocomplete_total",
    "Autocomplete events. accepted=false ~ rejected/cancelled (shown-but-not-accepted, "
    "NOT a true mid-stream cancel). cache_hit=true means Continue served the suggestion "
    "from its LRU cache WITHOUT calling the LLM — that's why a repeated request finishes "
    "instantly and shows up as 'Complete' instead of being cancellable.",
    ["accepted", "cache_hit", "user", "model", "provider"],
    registry=registry,
)
continue_last_event_timestamp = Gauge(
    "continue_last_event_timestamp_seconds",
    "Unix timestamp of the last accepted Continue event.",
    ["event_name", "user", "model", "provider"],
    registry=registry,
)
continue_ingest_errors_total = Counter(
    "continue_ingest_errors_total",
    "Number of ingest errors.",
    ["reason"],
    registry=registry,
)


def _ensure_dirs() -> None:
    JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_timestamp(data: dict[str, Any]) -> float:
    raw = data.get("timestamp") or data.get("time") or data.get("createdAt")
    if not raw:
        return datetime.now(timezone.utc).timestamp()
    try:
        if isinstance(raw, (int, float)):
            return float(raw)
        text = str(raw)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return datetime.now(timezone.utc).timestamp()


def _read_json_lines(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                rows.append(item)
    return rows


def _write_journal(payload: dict[str, Any]) -> None:
    with JOURNAL_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _unwrap(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Развернуть конверт Continue в (event_name, data).

    Continue шлёт {"name": ..., "data": {...}, "schema": ...}. Если этого конверта
    нет (другой клиент / старый формат) — трактуем payload как плоское событие.
    """
    inner = payload.get("data")
    if isinstance(inner, dict) and ("name" in payload or "schema" in payload):
        name = payload.get("name") or inner.get("eventName") or inner.get("type")
        return (str(name) if name else "unknown"), inner
    name = payload.get("name") or payload.get("eventName") or payload.get("type")
    return (str(name) if name else "unknown"), payload


def _first_number(data: dict[str, Any], keys: tuple[str, ...]) -> int:
    for key in keys:
        if key in data:
            try:
                return int(float(data[key]))
            except (TypeError, ValueError):
                return 0
    return 0


def _pick(data: dict[str, Any], *keys: str, default: str = "unknown") -> str:
    for key in keys:
        value = data.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return default


def _tri_bool(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    return "unknown"


def _event_id(event_name: str, data: dict[str, Any]) -> str:
    # В конверте нет стабильного id события. Дедупим по типу + содержимому
    # (timestamp в data обычно с миллисекундами, поэтому разные события различимы).
    raw = json.dumps({"name": event_name, "data": data}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _record_event(payload: dict[str, Any]) -> bool:
    event_name, data = _unwrap(payload)
    model = _pick(data, "model", "modelName")
    provider = _pick(data, "provider", "modelProvider")
    # userId — базовое поле всех событий dev-data 0.2.0; пустое при локальном VS Code
    # без логина в Continue Hub → "unknown".
    user = _pick(data, "userId", "user", "userEmail")
    event_key = _event_id(event_name, data)

    if event_key in seen_ids:
        return False

    seen_ids.add(event_key)
    _write_journal(payload)

    continue_events_total.labels(event_name=event_name, user=user, model=model, provider=provider).inc()
    continue_last_event_timestamp.labels(
        event_name=event_name, user=user, model=model, provider=provider
    ).set(_parse_timestamp(data))

    if event_name == "tokensGenerated":
        prompt_tokens = _first_number(data, ("promptTokens", "prompt_tokens", "inputTokens", "input_tokens"))
        generated_tokens = _first_number(
            data,
            ("generatedTokens", "generated_tokens", "completionTokens", "completion_tokens", "outputTokens", "output_tokens"),
        )
        continue_prompt_tokens_total.labels(user=user, model=model, provider=provider).inc(prompt_tokens)
        continue_generated_tokens_total.labels(user=user, model=model, provider=provider).inc(generated_tokens)
    elif event_name == "autocomplete":
        continue_autocomplete_total.labels(
            accepted=_tri_bool(data.get("accepted")),
            cache_hit=_tri_bool(data.get("cacheHit")),
            user=user,
            model=model,
            provider=provider,
        ).inc()

    return True


def _load_state() -> None:
    _ensure_dirs()
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            seen = state.get("seen_ids", [])
            if isinstance(seen, list):
                seen_ids.update(str(x) for x in seen)
        except Exception:
            continue_ingest_errors_total.labels(reason="state_load_failed").inc()

    for payload in _read_json_lines(JOURNAL_PATH):
        try:
            _record_event(payload)
        except Exception:
            continue_ingest_errors_total.labels(reason="journal_replay_failed").inc()


def _persist_state() -> None:
    state = {
        "updated_at": _now_iso(),
        "seen_ids": sorted(seen_ids),
    }
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


@app.on_event("startup")
def startup() -> None:
    _load_state()


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/ingest")
async def ingest(request: Request) -> Response:
    try:
        payload = await request.json()
    except Exception:
        continue_ingest_errors_total.labels(reason="invalid_json").inc()
        return Response(content='{"ok":false,"error":"invalid json"}', media_type="application/json", status_code=400)

    if isinstance(payload, dict):
        events = [payload]
    elif isinstance(payload, list):
        events = [item for item in payload if isinstance(item, dict)]
    else:
        continue_ingest_errors_total.labels(reason="unsupported_payload").inc()
        return Response(content='{"ok":false,"error":"unsupported payload"}', media_type="application/json", status_code=400)

    accepted = 0
    with lock:
        for event in events:
            try:
                event.setdefault("ingestedAt", _now_iso())
                if _record_event(event):
                    accepted += 1
            except Exception:
                continue_ingest_errors_total.labels(reason="record_failed").inc()
        _persist_state()

    return Response(content=json.dumps({"ok": True, "accepted": accepted}, ensure_ascii=False), media_type="application/json", status_code=200)


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
