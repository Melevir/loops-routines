# Security

claude_tab starts Claude Code agents that run tools on your Mac, so a hole in it is a hole in your machine.
Please report vulnerabilities privately.

## Reporting

Use GitHub's private reporting: **Security → Report a vulnerability** on
[the repository](https://github.com/Melevir/claude_tab/security/advisories/new). Don't open a public issue.
Expect a first answer within a week. Fixes go to the latest release only.

## What the daemon protects

| Boundary | How |
|---|---|
| Other machines | The daemon listens on `127.0.0.1` only. |
| Web pages in your browser (DNS rebinding, CSRF) | The `Host` header must be a loopback name; every API and MCP call needs a bearer token, which a page can't read. |
| Other local users | The token file (`~/Library/Application Support/claude_tab/token`) and the per-run MCP config are mode 0600. |
| One run posing as another | `/agent/mcp` accepts only a live run's own token, which dies with the run. |
| A routine widening its own permissions | Agents start with `--permission-mode dontAsk`, the allow list from `config.toml` and the routine's own `allowed_tools`; anything else is denied and recorded with the run. |

## What it does not protect

- **Anything running as you.** A process under your user can read the token file and use the API. This is
  the same trust boundary as your shell and your Claude Code sign-in.
- **What your routines allow.** A routine's prompt and `allowed_tools` are yours: an agent with
  `Bash(git push *)` can push. Review routine files before you switch them on, especially ones you did not write.
- **Prompt injection through the data a routine reads** (logs, PR descriptions, web pages). Keep the tools of
  routines that read untrusted text narrow.

Reports about these are still welcome when claude_tab makes them worse than they need to be.
