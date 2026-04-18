from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Any, cast

import aiohttp
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from prometheus_client.exposition import CONTENT_TYPE_LATEST

if TYPE_CHECKING:
    from collections.abc import Callable

REGISTRY = CollectorRegistry(auto_describe=True)

_HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
_TOOL_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0)
_LLM_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0)

_HTTP_REQUESTS = Counter(
    "axi_http_requests_total",
    "HTTP requests served by Axi's FastAPI app.",
    ("route", "method", "status"),
    registry=REGISTRY,
)
_HTTP_DURATION = Histogram(
    "axi_http_request_duration_seconds",
    "HTTP request duration for Axi's FastAPI app.",
    ("route", "method"),
    buckets=_HTTP_BUCKETS,
    registry=REGISTRY,
)

_DISCORD_REST_REQUESTS = Counter(
    "axi_discord_rest_requests_total",
    "Discord REST API request attempts by client, method, route, and status.",
    ("client", "method", "route", "status"),
    registry=REGISTRY,
)
_DISCORD_REST_DURATION = Histogram(
    "axi_discord_rest_request_duration_seconds",
    "Discord REST API request attempt duration by client, method, and route.",
    ("client", "method", "route"),
    buckets=_HTTP_BUCKETS,
    registry=REGISTRY,
)

_DISCORD_INBOUND_EVENTS = Counter(
    "axi_discord_inbound_events_total",
    "Inbound Discord events seen by Axi, labeled by event type and outcome.",
    ("event", "outcome"),
    registry=REGISTRY,
)

_LLM_REQUESTS = Counter(
    "axi_llm_requests_total",
    "LLM request outcomes by model.",
    ("model", "outcome"),
    registry=REGISTRY,
)
_LLM_DURATION = Histogram(
    "axi_llm_request_duration_seconds",
    "End-to-end LLM request duration by model and outcome.",
    ("model", "outcome"),
    buckets=_LLM_BUCKETS,
    registry=REGISTRY,
)
_LLM_API_DURATION = Histogram(
    "axi_llm_api_duration_seconds",
    "Claude API duration by model and outcome.",
    ("model", "outcome"),
    buckets=_LLM_BUCKETS,
    registry=REGISTRY,
)
_LLM_TOKENS = Counter(
    "axi_llm_tokens_total",
    "LLM token totals by model and token kind.",
    ("model", "kind"),
    registry=REGISTRY,
)
_LLM_COST_USD = Counter(
    "axi_llm_cost_usd_total",
    "Accumulated LLM cost in USD by model.",
    ("model",),
    registry=REGISTRY,
)
_LLM_SERVER_TOOL_REQUESTS = Counter(
    "axi_llm_server_tool_requests_total",
    "Claude server-side tool requests by model and tool.",
    ("model", "tool"),
    registry=REGISTRY,
)

_TOOL_CALLS = Counter(
    "axi_tool_calls_total",
    "Tool call outcomes by tool name.",
    ("tool_name", "outcome"),
    registry=REGISTRY,
)
_TOOL_DURATION = Histogram(
    "axi_tool_call_duration_seconds",
    "Tool call duration by tool name and outcome.",
    ("tool_name", "outcome"),
    buckets=_TOOL_BUCKETS,
    registry=REGISTRY,
)

_AGENT_MESSAGE_EVENTS = Counter(
    "axi_agent_message_events_total",
    "Agent message lifecycle events.",
    ("event",),
    registry=REGISTRY,
)

_AGENTS_TOTAL = Gauge(
    "axi_agents_total",
    "Number of registered Axi agent sessions.",
    registry=REGISTRY,
)
_AGENTS_AWAKE = Gauge(
    "axi_agents_awake",
    "Number of awake Axi agent sessions.",
    registry=REGISTRY,
)
_AGENTS_BUSY = Gauge(
    "axi_agents_busy",
    "Number of busy Axi agent sessions.",
    registry=REGISTRY,
)
_AGENT_MESSAGE_QUEUE_TOTAL = Gauge(
    "axi_agent_message_queue_messages",
    "Total queued messages across all agents.",
    registry=REGISTRY,
)
_AGENT_MESSAGE_QUEUE_MAX = Gauge(
    "axi_agent_message_queue_max",
    "Largest queued-message depth on any single agent.",
    registry=REGISTRY,
)
_SCHEDULER_WAITERS = Gauge(
    "axi_scheduler_waiters",
    "Number of agents waiting for scheduler slots.",
    registry=REGISTRY,
)
_SCHEDULER_SLOTS_IN_USE = Gauge(
    "axi_scheduler_slots_in_use",
    "Number of occupied scheduler slots.",
    registry=REGISTRY,
)
_SCHEDULER_YIELD_TARGETS = Gauge(
    "axi_scheduler_yield_targets",
    "Number of agents marked to yield after their current turn.",
    registry=REGISTRY,
)

_agent_sessions_provider: Callable[[], dict[str, Any]] = dict
_scheduler_status_provider: Callable[[], dict[str, Any]] = dict

_SNOWFLAKE_SEGMENT_RE = re.compile(r"/(?P<id>\d{5,})(?=/|$)")
_WEBHOOK_TOKEN_RE = re.compile(r"(/webhooks/:id/)[^/]+")
_WEBHOOK_TOKEN_RAW_RE = re.compile(r"(/webhooks/\d{5,}/)[^/]+")


def set_agent_sessions_provider(provider: Callable[[], dict[str, Any]]) -> None:
    global _agent_sessions_provider
    _agent_sessions_provider = provider



def set_scheduler_status_provider(provider: Callable[[], dict[str, Any]]) -> None:
    global _scheduler_status_provider
    _scheduler_status_provider = provider



def _get_sessions() -> list[Any]:
    try:
        sessions = _agent_sessions_provider()
    except Exception:
        return []
    return list(sessions.values())



def _queue_lengths() -> list[int]:
    return [len(getattr(session, "message_queue", ())) for session in _get_sessions()]



def _busy_count() -> int:
    count = 0
    for session in _get_sessions():
        query_lock = getattr(session, "query_lock", None)
        locked = bool(query_lock is not None and query_lock.locked())
        if locked or bool(getattr(session, "bridge_busy", False)):
            count += 1
    return count



def _awake_count() -> int:
    return sum(1 for session in _get_sessions() if getattr(session, "client", None) is not None)



def _scheduler_metric(key: str) -> float:
    try:
        status = _scheduler_status_provider()
    except Exception:
        return 0.0
    value = status.get(key, 0)
    if isinstance(value, list):
        items = cast("list[Any]", value)
        return float(len(items))
    return float(value)



def normalize_discord_route(route: str) -> str:
    route = route or "/"
    route = _SNOWFLAKE_SEGMENT_RE.sub("/:id", route)
    route = _WEBHOOK_TOKEN_RE.sub(r"\1:token", route)
    return _WEBHOOK_TOKEN_RAW_RE.sub(r"\1:token", route)



def observe_http_request(route: str, method: str, status: int | str, duration_seconds: float) -> None:
    route_label = route or "/"
    method_label = method.upper()
    status_label = str(status)
    _HTTP_REQUESTS.labels(route=route_label, method=method_label, status=status_label).inc()
    _HTTP_DURATION.labels(route=route_label, method=method_label).observe(max(duration_seconds, 0.0))



def observe_discord_rest_request(
    client: str,
    method: str,
    route: str,
    status: int | str,
    duration_seconds: float,
) -> None:
    route_label = normalize_discord_route(route)
    method_label = method.upper()
    status_label = str(status)
    _DISCORD_REST_REQUESTS.labels(
        client=client,
        method=method_label,
        route=route_label,
        status=status_label,
    ).inc()
    _DISCORD_REST_DURATION.labels(
        client=client,
        method=method_label,
        route=route_label,
    ).observe(max(duration_seconds, 0.0))



def observe_inbound_discord_event(event: str, outcome: str) -> None:
    _DISCORD_INBOUND_EVENTS.labels(event=event, outcome=outcome).inc()



def observe_llm_result(
    *,
    model: str | None,
    outcome: str,
    duration_ms: int | None = None,
    api_duration_ms: int | None = None,
    usage: dict[str, Any] | None = None,
    total_cost_usd: float | None = None,
) -> None:
    model_label = model or "unknown"
    _LLM_REQUESTS.labels(model=model_label, outcome=outcome).inc()

    if duration_ms is not None and duration_ms >= 0:
        _LLM_DURATION.labels(model=model_label, outcome=outcome).observe(duration_ms / 1000)
    if api_duration_ms is not None and api_duration_ms >= 0:
        _LLM_API_DURATION.labels(model=model_label, outcome=outcome).observe(api_duration_ms / 1000)

    usage = usage or {}
    for kind in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        value = usage.get(kind)
        if isinstance(value, (int, float)) and value > 0:
            _LLM_TOKENS.labels(model=model_label, kind=kind).inc(value)

    server_tool_use_raw = usage.get("server_tool_use")
    if isinstance(server_tool_use_raw, dict):
        server_tool_use = cast("dict[str, Any]", server_tool_use_raw)
        for tool_name, count in server_tool_use.items():
            if isinstance(count, (int, float)) and count > 0:
                _LLM_SERVER_TOOL_REQUESTS.labels(model=model_label, tool=str(tool_name)).inc(count)

    if total_cost_usd is not None and total_cost_usd > 0:
        _LLM_COST_USD.labels(model=model_label).inc(total_cost_usd)



def observe_tool_result(tool_name: str | None, duration_seconds: float | None, is_error: bool | None) -> None:
    name_label = tool_name or "unknown"
    outcome = "error" if is_error else "ok"
    _TOOL_CALLS.labels(tool_name=name_label, outcome=outcome).inc()
    if duration_seconds is not None and duration_seconds >= 0:
        _TOOL_DURATION.labels(tool_name=name_label, outcome=outcome).observe(duration_seconds)



def observe_agent_message_event(event: str) -> None:
    _AGENT_MESSAGE_EVENTS.labels(event=event).inc()



def render_latest_metrics() -> bytes:
    return generate_latest(REGISTRY)



def metrics_content_type() -> str:
    return CONTENT_TYPE_LATEST



def make_discord_http_trace_config(client_name: str = "discordpy") -> aiohttp.TraceConfig:
    trace_config = aiohttp.TraceConfig()

    async def on_request_start(
        _session: aiohttp.ClientSession,
        trace_config_ctx: Any,
        params: aiohttp.TraceRequestStartParams,
    ) -> None:
        trace_config_ctx.axi_started_at = time.monotonic()
        trace_config_ctx.axi_method = params.method
        trace_config_ctx.axi_route = normalize_discord_route(params.url.path)

    async def on_request_end(
        _session: aiohttp.ClientSession,
        trace_config_ctx: Any,
        params: aiohttp.TraceRequestEndParams,
    ) -> None:
        started_at = getattr(trace_config_ctx, "axi_started_at", time.monotonic())
        route = getattr(trace_config_ctx, "axi_route", normalize_discord_route(params.url.path))
        method = getattr(trace_config_ctx, "axi_method", params.method)
        observe_discord_rest_request(client_name, method, route, params.response.status, time.monotonic() - started_at)

    async def on_request_exception(
        _session: aiohttp.ClientSession,
        trace_config_ctx: Any,
        params: aiohttp.TraceRequestExceptionParams,
    ) -> None:
        started_at = getattr(trace_config_ctx, "axi_started_at", time.monotonic())
        route = getattr(trace_config_ctx, "axi_route", normalize_discord_route(params.url.path))
        method = getattr(trace_config_ctx, "axi_method", params.method)
        observe_discord_rest_request(client_name, method, route, "exception", time.monotonic() - started_at)

    trace_config.on_request_start.append(cast("Any", on_request_start))
    trace_config.on_request_end.append(cast("Any", on_request_end))
    trace_config.on_request_exception.append(cast("Any", on_request_exception))
    return trace_config


_AGENTS_TOTAL.set_function(lambda: float(len(_get_sessions())))
_AGENTS_AWAKE.set_function(lambda: float(_awake_count()))
_AGENTS_BUSY.set_function(lambda: float(_busy_count()))
_AGENT_MESSAGE_QUEUE_TOTAL.set_function(lambda: float(sum(_queue_lengths())))
_AGENT_MESSAGE_QUEUE_MAX.set_function(lambda: float(max(_queue_lengths(), default=0)))
_SCHEDULER_WAITERS.set_function(lambda: _scheduler_metric("waiters"))
_SCHEDULER_SLOTS_IN_USE.set_function(lambda: _scheduler_metric("slot_count"))
_SCHEDULER_YIELD_TARGETS.set_function(lambda: _scheduler_metric("yield_targets"))
