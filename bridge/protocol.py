"""Wire protocol models for the agent bridge.

Bridge-layer messages are typed Pydantic models. Claude SDK messages
(relayed via stdin/stdout) are kept as opaque dicts.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


# -- Bot.py → Bridge ----------------------------------------------------------

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
    data: dict[str, Any]  # opaque Claude SDK message


# -- Bridge → Bot.py ----------------------------------------------------------

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
    data: dict[str, Any]  # opaque Claude SDK message

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

ClientMsg = CmdMsg | StdinMsg
ServerMsg = ResultMsg | StdoutMsg | StderrMsg | ExitMsg

parse_client_msg = TypeAdapter(ClientMsg).validate_json
parse_server_msg = TypeAdapter(ServerMsg).validate_json
