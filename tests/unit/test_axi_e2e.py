from __future__ import annotations

from dataclasses import dataclass

from tests.axi_e2e import AxiDiscordEntrypoints


@dataclass
class ChannelStub:
    sent: list[tuple[str, float]]

    def send_and_wait(self, content: str, timeout: float = 120.0):
        self.sent.append((content, timeout))
        return type("Result", (), {"messages": [{"content": "ok"}]})()


def test_axi_entrypoints_format_spawn_kill_and_message() -> None:
    master = ChannelStub(sent=[])
    axi = AxiDiscordEntrypoints(master=master)  # type: ignore[arg-type]

    axi.spawn_agent(
        name="worker",
        cwd="/tmp/worker",
        prompt="Say exactly: READY",
        command="prompt",
        command_args='"prefix text"',
        resume="sid-1",
        packs=["ext-a"],
        timeout=180.0,
    )
    axi.kill_agent("worker", timeout=60.0)
    axi.send_to_agent("worker", "Say exactly: LATER", timeout=45.0)

    assert master.sent == [
        (
            'Spawn an agent named "worker" with cwd "/tmp/worker" and prompt "Say exactly: READY" and resume="sid-1" and command="prompt" and command_args="\"prefix text\"" and packs=[\'ext-a\']',
            180.0,
        ),
        ('Kill the agent named "worker"', 60.0),
        ('Send a message to the agent "worker" saying: "Say exactly: LATER"', 45.0),
    ]
