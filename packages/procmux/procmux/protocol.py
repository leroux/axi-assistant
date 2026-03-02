"""Wire protocol models for procmux.

Messages between the procmux server and its clients are typed Pydantic models.
Subprocess payloads (relayed via stdin/stdout) are kept as opaque dicts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

# -- Client → Server ----------------------------------------------------------


class CmdMsg(BaseModel):
    type: Literal["cmd"] = "cmd"
    cmd: str
    name: str = ""
    cli_args: list[str] = []
    env: dict[str, str] = {}
    cwd: str | None = None


class StdinMsg(BaseModel):
    type: Literal["stdin"] = "stdin"
    name: str
    data: dict[str, Any]  # opaque payload forwarded to subprocess


# -- Server → Client ----------------------------------------------------------


class ResultMsg(BaseModel):
    type: Literal["result"] = "result"
    ok: bool
    name: str = ""
    error: str | None = None
    # spawn
    pid: int | None = None
    already_running: bool | None = None
    # subscribe
    replayed: int | None = None
    status: str | None = None
    exit_code: int | None = None
    idle: bool | None = None
    # list
    agents: dict[str, Any] | None = None
    # status
    uptime_seconds: int | None = None


class StdoutMsg(BaseModel):
    type: Literal["stdout"] = "stdout"
    name: str
    data: dict[str, Any]  # opaque payload from subprocess


class StderrMsg(BaseModel):
    type: Literal["stderr"] = "stderr"
    name: str
    text: str


class ExitMsg(BaseModel):
    type: Literal["exit"] = "exit"
    name: str
    code: int | None = None


# -- Discriminated union helpers -----------------------------------------------


from pydantic import TypeAdapter

if TYPE_CHECKING:
    from collections.abc import Callable

ClientMsg = CmdMsg | StdinMsg
ServerMsg = ResultMsg | StdoutMsg | StderrMsg | ExitMsg

_client_adapter: TypeAdapter[ClientMsg] = TypeAdapter(ClientMsg)
_server_adapter: TypeAdapter[ServerMsg] = TypeAdapter(ServerMsg)

parse_client_msg: Callable[[str | bytes], ClientMsg] = _client_adapter.validate_json
parse_server_msg: Callable[[str | bytes], ServerMsg] = _server_adapter.validate_json
