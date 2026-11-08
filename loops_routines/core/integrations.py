"""What a run may use besides its built-in tools: Claude Code plugins (skills, commands, MCP servers),
claude.ai connectors of the agent's account, standalone skills and the user's own MCP servers.
(Taken from Loops' integrations.py; a routine names what it needs.)

Discovery reads what Claude Code keeps on disk (installed_plugins.json, each plugin's .mcp.json,
~/.claude/skills) plus `claude mcp list` run as the agent's account, for connector URLs and statuses.

A run gets exactly the servers its routine selects. Claude Code's `allowedMcpServers` (passed with
--settings) blocks every server that matches no entry; entries match stdio servers by command line and
remote ones by URL (claude.ai connectors match by URL only). Org-managed servers ignore that list, so the
ones not selected are blocked by name with `deniedMcpServers`. Plugins installed in the agent's account
are switched on by id with `enabledPlugins`, so their secrets (keychain, keyed by plugin id) reach their
servers; any other plugin loads with --plugin-dir as `<name>@inline`, without secrets. Selected
standalone skills are linked into a generated plugin. Plugins with hooks are never loaded: hooks rewrite
commands and break the permission rules.
"""

import asyncio
import json
import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

CLAUDE_HOME = Path.home() / ".claude"
SKILLS_PLUGIN = "loops-routines-skills"
PLUGIN_ROOT = "${CLAUDE_PLUGIN_ROOT}"
STATUS = {"✔": "connected", "!": "needs-auth", "✘": "failed", "⏸": "pending"}


@dataclass
class Server:
    name: str  # as Claude Code names it: plugin:<plugin>:<server>, "claude.ai <Name>", or the own name
    kind: str  # plugin | connector | custom | managed
    match: dict | None = None  # allowedMcpServers entry: {"serverCommand": [...]} or {"serverUrl": url}
    plugin: str = ""
    target: str = ""
    status: str = ""  # connected | needs-auth | failed | pending | "" (unknown) from the last scan
    spec: dict | None = None  # .mcp.json entry, for custom servers (they go into --mcp-config)


@dataclass
class Plugin:
    name: str
    id: str  # name@marketplace
    version: str
    path: str
    skills: list[str] = field(default_factory=list)
    servers: list[str] = field(default_factory=list)
    has_hooks: bool = False
    secrets: list[str] = field(default_factory=list)  # sensitive userConfig keys (kept in the keychain)


@dataclass
class RunSetup:
    """What a run adds to the claude command line for its routine's integrations."""
    argv: list[str] = field(default_factory=list)  # --plugin-dir ..., --settings <file>
    allowed: list[str] = field(default_factory=list)  # permission rules for the selected servers and skills
    mcp_servers: dict = field(default_factory=dict)  # own servers for --mcp-config
    servers: list[str] = field(default_factory=list)  # selected server names, for the prompt
    skills: list[str] = field(default_factory=list)  # skill names as the agent invokes them
    problems: list[str] = field(default_factory=list)  # selected but not usable, with why


def tool_prefix(server_name: str) -> str:
    """Permission rule covering every tool of a server: mcp__plugin_observability_kibana."""
    return "mcp__" + re.sub(r"[^A-Za-z0-9_-]", "_", server_name)


def _match(spec: dict) -> tuple[dict | None, str]:
    if spec.get("url"):
        return {"serverUrl": spec["url"]}, spec["url"]
    if spec.get("command"):
        command = [spec["command"], *[str(a) for a in spec.get("args", [])]]
        return {"serverCommand": command}, " ".join(command)
    return None, ""


def _expand(value, root: str):
    if isinstance(value, str):
        return value.replace(PLUGIN_ROOT, root)
    if isinstance(value, list):
        return [_expand(v, root) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v, root) for k, v in value.items()}
    return value


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _plugin_mcp(path: Path, manifest: dict) -> dict:
    """A plugin's MCP servers: .mcp.json, or mcpServers in plugin.json (inline or a relative path)."""
    inline = manifest.get("mcpServers")
    if isinstance(inline, dict):
        return inline
    source = path / inline if isinstance(inline, str) else path / ".mcp.json"
    servers = _read_json(source).get("mcpServers", {})
    return servers if isinstance(servers, dict) else {}


def installed_plugins(home: Path = CLAUDE_HOME) -> list[Plugin]:
    """Plugins installed in Claude Code (the user-scope install when there are several)."""
    registry = _read_json(home / "plugins" / "installed_plugins.json").get("plugins", {})
    plugins = []
    for plugin_id, installs in sorted(registry.items()):
        installs = [i for i in installs or [] if isinstance(i, dict) and Path(i.get("installPath", "")).is_dir()]
        if not installs:
            continue
        install = next((i for i in installs if i.get("scope") == "user"), installs[-1])
        path = Path(install["installPath"])
        manifest = _read_json(path / ".claude-plugin" / "plugin.json")
        name = manifest.get("name") or plugin_id.split("@")[0]
        listed_skills = manifest.get("skills")
        if isinstance(listed_skills, list):
            folders = [path / str(p) for p in listed_skills]
        else:
            folders = list((path / "skills").iterdir()) if (path / "skills").is_dir() else []
        skills = sorted(p.name for p in folders if (p / "SKILL.md").is_file())
        plugins.append(Plugin(
            name=name, id=plugin_id, version=str(install.get("version", "")), path=str(path), skills=skills,
            servers=[f"plugin:{name}:{server}" for server in _plugin_mcp(path, manifest)],
            has_hooks=(path / "hooks" / "hooks.json").is_file() or bool(manifest.get("hooks")),
            secrets=[key for key, spec in (manifest.get("userConfig") or {}).items()
                     if isinstance(spec, dict) and spec.get("sensitive")],
        ))
    return plugins


def plugin_servers(plugin: Plugin) -> list[Server]:
    path = Path(plugin.path)
    specs = _plugin_mcp(path, _read_json(path / ".claude-plugin" / "plugin.json"))
    servers = []
    for name, spec in specs.items():
        match, target = _match(_expand(spec, plugin.path))
        servers.append(Server(f"plugin:{plugin.name}:{name}", "plugin", match, plugin.name, target))
    return servers


def standalone_skills(home: Path = CLAUDE_HOME) -> dict[str, str]:
    """~/.claude/skills: name -> real folder (entries are often symlinks into a project)."""
    root = home / "skills"
    if not root.is_dir():
        return {}
    return {p.name: str(p.resolve()) for p in sorted(root.iterdir())
            if not p.name.startswith(".") and (p / "SKILL.md").is_file()}


def custom_servers(path: str) -> tuple[list[Server], str | None]:
    """The user's own servers from a file in .mcp.json format; (servers, problem)."""
    if not path.strip():
        return [], None
    source = Path(path).expanduser()
    if not source.is_file():
        return [], f"MCP config file {path} not found"
    specs = _read_json(source).get("mcpServers")
    if not isinstance(specs, dict):
        return [], f"MCP config file {path}: expected {{\"mcpServers\": {{...}}}}"
    servers = []
    for name, spec in specs.items():
        match, target = _match(spec if isinstance(spec, dict) else {})
        servers.append(Server(name, "custom", match, target=target, spec=spec))
    return servers, None


def managed_server_names(account_home: Path) -> list[str]:
    """Servers the organisation pushes (remote/managed settings): they ignore allowedMcpServers."""
    names = []
    for source in (account_home / "remote-settings.json",
                   Path("/Library/Application Support/ClaudeCode/managed-mcp.json"),
                   Path("/Library/Application Support/ClaudeCode/managed-settings.json")):
        data = _read_json(source)
        for key in ("managedMcpServers", "mcpServers"):
            if isinstance(data.get(key), dict):
                names += [n for n in data[key] if n not in names]
    return names


def parse_mcp_list(text: str) -> dict[str, dict]:
    """`claude mcp list` lines "<name>: <command or URL> - <mark> <status>" -> {name: {target, status}}."""
    listed = {}
    for line in text.splitlines():
        head, sep, tail = line.rpartition(" - ")
        name, colon, target = head.partition(": ")
        if not (sep and colon and name.strip()):
            continue
        mark = tail.strip()[:1]
        listed[name.strip()] = {"target": target.strip(), "status": STATUS.get(mark, tail.strip()[:40])}
    return listed


async def scan(command: str, env: dict, cwd: Path, timeout: float = 120) -> dict[str, dict]:
    """Run `claude mcp list` as the agent's account (env carries its CLAUDE_CONFIG_DIR). cwd must not be a
    project folder, so no project .mcp.json gets in."""
    cwd.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        command, "mcp", "list", cwd=cwd, env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        raise RuntimeError(f"`claude mcp list` took longer than {timeout:.0f}s") from None
    listed = parse_mcp_list(out.decode(errors="replace"))
    if proc.returncode and not listed:
        raise RuntimeError(f"`claude mcp list` failed: {out.decode(errors='replace').strip()[-300:]}")
    return listed


def connectors(listed: dict[str, dict]) -> list[Server]:
    return [Server(name, "connector", {"serverUrl": item["target"]}, target=item["target"], status=item["status"])
            for name, item in listed.items() if name.startswith("claude.ai ") and item["target"].startswith("http")]


def inventory(account_home: Path, mcp_config_file: str, listed: dict[str, dict] | None,
              home: Path = CLAUDE_HOME) -> dict:
    """Everything a routine can name, with statuses from the last scan (may be None)."""
    listed = listed or {}
    plugins = installed_plugins(home)
    servers = [s for p in plugins if not p.has_hooks for s in plugin_servers(p)]
    servers += connectors(listed)
    own, problem = custom_servers(mcp_config_file)
    servers += own
    servers += [Server(n, "managed", None, status=listed.get(n, {}).get("status", ""))
                for n in managed_server_names(account_home)]
    for server in servers:
        server.status = server.status or listed.get(server.name, {}).get("status", "")
    return {
        "plugins": [asdict(p) for p in plugins],
        "servers": [{k: v for k, v in asdict(s).items() if k not in ("match", "spec")} for s in servers],
        "skills": [{"name": n, "path": p} for n, p in standalone_skills(home).items()],
        "problems": [problem] if problem else [],
    }


def read_plugin_options(home: Path = CLAUDE_HOME) -> dict:
    """Non-secret plugin options (pluginConfigs) from the user's settings; secrets stay in the keychain."""
    configs = _read_json(home / "settings.json").get("pluginConfigs", {})
    return configs if isinstance(configs, dict) else {}


def write_skills_plugin(skills: dict[str, str], folder: Path) -> Path:
    """A throwaway plugin whose skills/ links to the selected skill folders."""
    if folder.exists():
        shutil.rmtree(folder)
    (folder / ".claude-plugin").mkdir(parents=True)
    (folder / "skills").mkdir()
    (folder / ".claude-plugin" / "plugin.json").write_text(json.dumps(
        {"name": SKILLS_PLUGIN, "version": "0.0.0", "description": "Skills loops-routines hands to a routine"}))
    for name, path in skills.items():
        os.symlink(path, folder / "skills" / name)
    return folder


def plan_run(*, plugins_wanted, servers_wanted, skills_wanted, mcp_config_file: str, agent_url: str,
             listed: dict[str, dict] | None, account_home: Path, out_dir: Path, home: Path = CLAUDE_HOME) -> RunSetup:
    """Turn a routine's plugins / mcp_servers / skills into command-line pieces; files go to out_dir (the
    run's own folder, so parallel runs never share them)."""
    setup = RunSetup()
    plugins = {p.name: p for p in installed_plugins(home)}
    agent_plugins = {p.name: p for p in installed_plugins(account_home) if not p.has_hooks}
    known: dict[str, Server] = {}
    for plugin in [*plugins.values(), *agent_plugins.values()]:
        if not plugin.has_hooks:
            known.update({s.name: s for s in plugin_servers(plugin)})
    known.update({s.name: s for s in connectors(listed or {})})
    own, problem = custom_servers(mcp_config_file)
    if problem:
        setup.problems.append(problem)
    known.update({s.name: s for s in own})
    managed = managed_server_names(account_home)

    allow = [{"serverUrl": agent_url}]  # loops-routines's own server for the agent
    load = list(plugins_wanted)
    for name in servers_wanted:
        server = known.get(name)
        if name in managed:
            setup.servers.append(name)
        elif server is None or server.match is None:
            hint = "scan integrations (loops-routines integrations --scan)" if name.startswith("claude.ai ") \
                else "not installed"
            setup.problems.append(f"MCP server {name}: {hint}")
            continue
        else:
            allow.append(server.match)
            setup.servers.append(name)
            if server.plugin and server.plugin not in load:
                load.append(server.plugin)  # a plugin's server needs the plugin loaded
            if server.kind == "custom":
                setup.mcp_servers[server.name] = server.spec
        setup.allowed.append(tool_prefix(name))

    options = read_plugin_options(home)
    agent_options = read_plugin_options(account_home)
    plugin_configs, enabled = {}, {}
    for name in load:
        plugin = plugins.get(name)
        if plugin is not None and plugin.has_hooks:
            setup.problems.append(f"plugin {name}: has hooks, which would rewrite the agent's commands; skipped")
            continue
        installed = agent_plugins.get(name)
        if installed is None and plugin is None:
            setup.problems.append(f"plugin {name}: not installed")
            continue
        if installed is not None:
            plugin = installed
            enabled[plugin.id] = True
            own_options = agent_options.get(plugin.id) or options.get(plugin.id)
            if isinstance(own_options, dict):
                plugin_configs[plugin.id] = own_options
        else:
            if plugin.secrets:
                setup.problems.append(
                    f"plugin {name}: not installed in this run's Claude account ({account_home}), so its "
                    f"secret options ({', '.join(plugin.secrets)}) can't reach it; install it there and enter "
                    f"them once: CLAUDE_CONFIG_DIR={account_home} claude, then /plugin")
            setup.argv += ["--plugin-dir", plugin.path]
            if isinstance(options.get(plugin.id), dict):
                plugin_configs[f"{plugin.name}@inline"] = options[plugin.id]
        setup.allowed.append(f"Read(/{plugin.path}/**)")  # its skills read their references
        setup.skills += [f"{plugin.name}:{skill}" for skill in plugin.skills]

    available = standalone_skills(home)
    chosen = {}
    for name in skills_wanted:
        path = available.get(name) or (str(Path(name).expanduser().resolve())
                                       if (Path(name).expanduser() / "SKILL.md").is_file() else None)
        if path is None:
            setup.problems.append(f"skill {name}: not found in {home / 'skills'}")
            continue
        chosen[Path(name).name] = path
    if chosen:
        folder = write_skills_plugin(chosen, out_dir / "skills-plugin")
        setup.argv += ["--plugin-dir", str(folder)]
        setup.allowed += [f"Read(/{path}/**)" for path in chosen.values()]
        setup.skills += [f"{SKILLS_PLUGIN}:{name}" for name in chosen]
    if setup.skills:
        setup.allowed.append("Skill")

    settings = {"allowedMcpServers": allow,
                "deniedMcpServers": [{"serverName": n} for n in managed if n not in servers_wanted]}
    if enabled:
        settings["enabledPlugins"] = enabled
    if plugin_configs:
        settings["pluginConfigs"] = plugin_configs
    out_dir.mkdir(parents=True, exist_ok=True)
    settings_path = out_dir / "settings.json"
    settings_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False))
    setup.argv += ["--settings", str(settings_path)]
    return setup


def describe(setup: RunSetup) -> str:
    """The run's integrations, for its prompt."""
    if not (setup.servers or setup.skills):
        return ""
    lines = ["\n\nIntegrations available in this run:"]
    if setup.servers:
        lines.append("- MCP servers: " + ", ".join(setup.servers))
    if setup.skills:
        lines.append("- skills: " + ", ".join(setup.skills))
    return "\n".join(lines)
