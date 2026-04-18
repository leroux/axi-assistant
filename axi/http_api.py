"""Minimal HTTP API for triggering agent sessions from external processes.

Provides a single POST /v1/trigger endpoint that spawns or routes to an agent.
Started as an asyncio task inside the bot's event loop when HTTP_API_PORT != 0.
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from axi import agents, config
from axi.metrics import metrics_content_type, observe_http_request, render_latest_metrics

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

log = logging.getLogger("axi")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def prometheus_http_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    started_at = time.monotonic()
    route_label = request.url.path
    try:
        response = await call_next(request)
    except Exception:
        observe_http_request(route_label, request.method, 500, time.monotonic() - started_at)
        raise
    observe_http_request(route_label, request.method, response.status_code, time.monotonic() - started_at)
    return response


class TriggerRequest(BaseModel):
    session: str
    prompt: str
    cwd: str | None = None
    extensions: list[str] | None = None
    mcp_servers: list[str] | None = None


@app.get("/metrics")
async def metrics() -> Response:
    return Response(content=render_latest_metrics(), media_type=metrics_content_type())


@app.post("/v1/trigger")
async def trigger(req: TriggerRequest):
    agent_name = req.session
    agent_cwd = req.cwd or os.path.join(config.AXI_USER_DATA, "agents", agent_name)

    try:
        if agent_name in agents.agents:
            log.info("HTTP trigger: routing to existing session '%s'", agent_name)
            await agents.send_prompt_to_agent(agent_name, req.prompt)
            return {"status": "ok", "action": "routed"}

        log.info("HTTP trigger: spawning new session '%s'", agent_name)
        await agents.reclaim_agent_name(agent_name)
        extra_mcp = config.load_mcp_servers(req.mcp_servers) if req.mcp_servers else None
        await agents.spawn_agent(
            agent_name,
            agent_cwd,
            req.prompt,
            extensions=req.extensions,
            extra_mcp_servers=extra_mcp,
        )
        return {"status": "ok", "action": "spawned"}
    except Exception:
        log.exception("HTTP trigger failed for session '%s'", agent_name)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": f"Failed to trigger session '{agent_name}'"},
        )
