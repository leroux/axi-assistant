"""Wire protocol models for the agent bridge.

Bridge-layer messages are typed Pydantic models. Claude SDK messages
(relayed via stdin/stdout) are kept as opaque dicts.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class _DictAccessModel(BaseModel):
    """Base model that supports both attribute and dict-style access."""
    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def __contains__(self, key: str) -> bool:
        return hasattr(self, key) and getattr(self, key) is not None


# -- Bot.py → Bridge ----------------------------------------------------------

class CmdMsg(_DictAccessModel):
    type: Literal["cmd"] = "cmd"
    cmd: str
    name: str = ""
    cli_args: list[str] = []
    env: dict[str, str] = {}
    cwd: str | None = None

class StdinMsg(_DictAccessModel):
    type: Literal["stdin"] = "stdin"
    name: str
    data: dict[str, Any]  # opaque Claude SDK message


# -- Bridge → Bot.py ----------------------------------------------------------

class ResultMsg(_DictAccessModel):
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

class StdoutMsg(_DictAccessModel):
    type: Literal["stdout"] = "stdout"
    name: str
    data: dict[str, Any]  # opaque Claude SDK message

class StderrMsg(_DictAccessModel):
    type: Literal["stderr"] = "stderr"
    name: str
    text: str

class ExitMsg(_DictAccessModel):
    type: Literal["exit"] = "exit"
    name: str
    code: int | None = None


# -- Discriminated union helpers -----------------------------------------------

from pydantic import TypeAdapter

ClientMsg = CmdMsg | StdinMsg
ServerMsg = ResultMsg | StdoutMsg | StderrMsg | ExitMsg

parse_client_msg = TypeAdapter(ClientMsg).validate_json
parse_server_msg = TypeAdapter(ServerMsg).validate_json
