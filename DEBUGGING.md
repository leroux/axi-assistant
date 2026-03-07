# Debugging Axi

## Signal-Based Stack Dumps

Send signals to the bot process to dump runtime state without interrupting it.

### Find the bot PID

```bash
ps -ef | grep "axi.main" | grep -v grep | grep -v uv
```

### SIGUSR1 — Thread Stacks

Dumps all thread stack traces (useful for finding deadlocks or stuck synchronous code).

```bash
kill -USR1 <pid>
cat /home/ubuntu/axi-assistant/logs/stack-dump.txt
```

### SIGUSR2 — Asyncio Tasks

Dumps all pending asyncio tasks with their coroutine stacks (useful for finding stuck async operations).

```bash
kill -USR2 <pid>
cat /home/ubuntu/axi-assistant/logs/asyncio-dump.txt
```

Both signals also print to stderr (visible in `journalctl -u axi-bot`).

## py-spy (External Profiler)

Attach to a running process without modifying it. Useful for diagnosing high CPU.

```bash
sudo py-spy top --pid <pid>        # live top-like view
sudo py-spy dump --pid <pid>       # one-shot stack dump
sudo py-spy record --pid <pid> -o profile.svg  # flame graph
```

## faulthandler

Enabled at startup. On segfaults or fatal errors, Python will print a traceback to stderr automatically.
