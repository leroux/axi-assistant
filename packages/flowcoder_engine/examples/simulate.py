"""Simulate engine execution and print what stdout would look like."""

import asyncio
import json

from flowcoder_engine.protocol import ProtocolHandler
from flowcoder_engine.session import QueryResult
from flowcoder_engine.walker import GraphWalker
from flowcoder_flowchart import load_command


# Mock session that returns canned responses
class FakeSession:
    def __init__(self, name, responses):
        self.name = name
        self.session_id = f"session-{name}"
        self.total_cost = 0.0
        self._responses = responses
        self._i = 0

    async def query(self, prompt, block_id="", block_name=""):
        idx = min(self._i, len(self._responses) - 1)
        self._i += 1
        self.total_cost += 0.03
        return QueryResult(
            response_text=self._responses[idx],
            cost_usd=0.03,
            duration_ms=1200,
        )


class FakePool:
    def __init__(self, session_responses):
        self._sr = session_responses
        self._sessions = {}

    async def get(self, name):
        if name not in self._sessions:
            self._sessions[name] = FakeSession(name, self._sr.get(name, ["OK"]))
        return self._sessions[name]

    async def reset(self, name):
        self._sessions.pop(name, None)

    async def stop_all(self):
        self._sessions.clear()

    @property
    def total_cost(self):
        return sum(s.total_cost for s in self._sessions.values())

    @property
    def sessions(self):
        return dict(self._sessions)


# Capture stdout instead of writing to actual stdout
class StdoutCapture(ProtocolHandler):
    def __init__(self):
        self._captured = []
        self._logs = []

    def emit(self, msg):
        self._captured.append(msg)

    def emit_block_start(self, block_id, block_name, block_type):
        msg = {
            "type": "system",
            "subtype": "block_start",
            "data": {"block_id": block_id, "block_name": block_name, "block_type": block_type},
        }
        self._captured.append(msg)

    def emit_block_complete(self, block_id, block_name, success):
        msg = {
            "type": "system",
            "subtype": "block_complete",
            "data": {"block_id": block_id, "block_name": block_name, "success": success},
        }
        self._captured.append(msg)

    def emit_result(self, result_text, is_error=False, duration_ms=0, num_turns=0, total_cost_usd=0.0):
        msg = {
            "type": "result",
            "subtype": "error" if is_error else "complete",
            "session_id": "flowchart",
            "duration_ms": duration_ms,
            "is_error": is_error,
            "num_turns": num_turns,
            "total_cost_usd": total_cost_usd,
            "result": result_text,
        }
        self._captured.append(msg)

    def emit_forwarded(self, inner_msg, session_name, block_id, block_name):
        self._captured.append({
            "type": "system",
            "subtype": "session_message",
            "data": {
                "session": session_name,
                "block_id": block_id,
                "block_name": block_name,
                "message": inner_msg,
            },
        })

    def log(self, message):
        self._logs.append(message)

    async def start(self):
        pass

    async def stop(self):
        pass


async def run_example(example_file, args, session_responses):
    cmd = load_command(example_file)
    pool = FakePool(session_responses)
    proto = StdoutCapture()
    walker = GraphWalker(cmd.flowchart, pool, args, proto)
    result = await walker.run()
    proto.emit_result(
        json.dumps(result.variables),
        is_error=result.status != "completed",
        duration_ms=result.duration_ms,
        num_turns=len(result.log),
        total_cost_usd=pool.total_cost,
    )
    return proto._captured, proto._logs


def print_session(title, captured, logs):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")
    print()
    print("── stderr (engine logs) ──")
    for log in logs:
        print(f"  [flowcoder] {log}")
    print()
    print("── stdout (protocol messages, one JSON per line) ──")
    for msg in captured:
        print(json.dumps(msg))
    print()


async def main():
    # Example 1: Simple chat passthrough
    captured, logs = await run_example(
        "examples/simple.json",
        {"$1": "What is 2+2?"},
        {"default": ["2+2 equals 4."]},
    )
    print_session("Example 1: Simple chat (start → prompt → end)", captured, logs)

    # Example 2: Multi-session code review (approved)
    captured, logs = await run_example(
        "examples/multi_session.json",
        {"$1": "fibonacci function"},
        {
            "coder": ["def fib(n):\n    if n <= 1: return n\n    return fib(n-1) + fib(n-2)"],
            "reviewer": ['{"approved": true, "feedback": "Clean recursive implementation."}'],
        },
    )
    print_session("Example 2: Code review (approved first try)", captured, logs)

    # Example 3: Multi-session code review (rejected then approved)
    captured, logs = await run_example(
        "examples/multi_session.json",
        {"$1": "hello world function"},
        {
            "coder": [
                "def hello(): print('hi')",
                "def hello(name='World'):\n    \"\"\"Greet someone.\"\"\"\n    print(f'Hello, {name}!')",
            ],
            "reviewer": [
                '{"approved": false, "feedback": "Add parameter and docstring."}',
                '{"approved": true, "feedback": "Much better with the parameter and docstring."}',
            ],
        },
    )
    print_session("Example 3: Code review (rejected → revised → approved)", captured, logs)


asyncio.run(main())
