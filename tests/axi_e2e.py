from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from discord_e2e import DiscordChannel


@dataclass(slots=True)
class AxiDiscordEntrypoints:
    master: DiscordChannel

    def spawn_agent(
        self,
        *,
        name: str,
        cwd: str,
        prompt: str,
        timeout: float = 180.0,
        resume: str | None = None,
        command: str | None = None,
        command_args: str | None = None,
        packs: list[str] | None = None,
    ) -> list[dict]:
        message = f'Spawn an agent named "{name}" with cwd "{cwd}" and prompt "{prompt}"'
        if resume:
            message += f' and resume="{resume}"'
        if command:
            message += f' and command="{command}"'
        if command_args:
            message += f' and command_args="{command_args}"'
        if packs is not None:
            message += f" and packs={packs!r}"
        return self.master.send_and_wait(message, timeout=timeout).messages

    def kill_agent(self, name: str, *, timeout: float = 60.0) -> list[dict]:
        return self.master.send_and_wait(f'Kill the agent named "{name}"', timeout=timeout).messages

    def send_to_agent(self, name: str, message: str, *, timeout: float = 60.0) -> list[dict]:
        content = f'Send a message to the agent "{name}" saying: "{message}"'
        return self.master.send_and_wait(content, timeout=timeout).messages
