"""Fresh usage numbers without a run: one tiny request (`claude -p`, haiku, no tools, no MCP, nothing
saved) as the configured account, reading the `rate_limit_event` line it prints. (Taken from Loops.)"""

import asyncio
import json
import tempfile

from loops_routines.core.claude_cli import agent_env, resolve_command
from loops_routines.core.clock import utc_now
from loops_routines.core.config import AgentConfig
from loops_routines.core.usage import snapshot_from_rate_limit_event

PROMPT = "Reply with the single word OK."
TIMEOUT_SECONDS = 90


def probe_argv(command: str) -> list[str]:
    return [
        command, "-p", PROMPT,
        "--output-format", "stream-json", "--verbose",
        "--model", "haiku", "--max-turns", "1",
        "--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers": {}}',
        "--setting-sources", "project,local",
        "--no-session-persistence",
        "--permission-mode", "dontAsk", "--permission-prompts", "none",
    ]


async def probe_usage(cfg: AgentConfig, timeout: float = TIMEOUT_SECONDS) -> dict:
    """The account's usage snapshot (source "probe"); RuntimeError with a readable reason otherwise."""
    command = resolve_command(cfg.command)
    with tempfile.TemporaryDirectory(prefix="loops-routines-probe-") as cwd:
        proc = await asyncio.create_subprocess_exec(
            *probe_argv(command), cwd=cwd, env=agent_env(cfg), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, limit=4 * 1024 * 1024,
        )
        snapshot, error, text = None, None, []

        async def read():
            nonlocal snapshot, error
            async for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                try:
                    event = json.loads(line)
                except ValueError:
                    text.append(line)  # e.g. "Not logged in" printed as plain text
                    continue
                snapshot = snapshot_from_rate_limit_event(event, utc_now(), "probe") or snapshot
                if isinstance(event, dict) and event.get("type") == "result" and event.get("is_error"):
                    error = str(event.get("result") or event.get("subtype") or "error")

        try:
            await asyncio.wait_for(asyncio.gather(read(), proc.wait()), timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"usage probe timed out after {timeout:.0f}s") from None
    if snapshot:
        return snapshot
    reason = error or " ".join(t for t in text if t)[:200] or f"exit {proc.returncode}"
    raise RuntimeError(f"usage probe got no limits ({reason}); is the account logged in to a Claude plan?")
