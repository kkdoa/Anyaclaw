# Security model

Anyaclaw executes model-authored file operations and shell commands on your machine. This page
describes what is enforced, what is only discouraged, and how to expose the web console safely.

## Trust boundaries

| Boundary | Enforced by | Notes |
|---|---|---|
| Model → filesystem | `resolve_workspace_path` in every file tool | `Path.resolve()` then boundary check; symlinks cannot escape |
| Model → shell | `ExecTool` guards + policy | deny patterns, optional allow-list, timeout, scrubbed env, tree kill |
| Model → network | `WebFetchTool` | http(s) only; private/loopback/link-local addresses refused |
| Model → side effects | `ToolPolicy` + `ApprovalBroker` | `exec`, `delete_path`, `send_mail`-style tools default to `confirm` |
| Remote user → agent | sender allow-list | Off by default; enable `enforce_sender_whitelist` for public bots |
| Browser → API | `web_auth_token` | Bearer token or `?token=`; `/api/health` stays public |
| Secrets → logs | `redact()` | API keys, bearer tokens and `key: value` pairs are masked |

## Path sandbox

```python
root = Path(workspace).resolve()
candidate = (root / raw).resolve()
if root not in candidate.parents and candidate != root:
    raise ToolError("path escapes the workspace")
```

Consequences:

* `../../etc/passwd`, `C:\Windows\System32`, and symlinks pointing outside are rejected.
* Absolute paths inside the workspace are allowed.
* Deleting the workspace root is refused explicitly.
* `security.workspace` is the **only** writable area. If you need a wider area, point it at a
  dedicated directory rather than disabling the check — there is no disable switch.

## Tool policy

```toml
[security]
tool_policy = "confirm"          # allow | confirm | deny
tool_policies = { exec = "confirm", delete_path = "confirm" }
auto_approve = false
```

* Read-only tools are always allowed and may run in parallel.
* `confirm` asks the active channel:
  * **CLI** — `run this? [y/N]` prompt;
  * **Web** — approval card backed by `ApprovalBroker` (`/api/approvals`);
  * **QQ / Feishu / DingTalk** — auto-deny unless `auto_approve = true`, because an unattended group
    chat cannot meaningfully consent.
* A timeout (5 min) resolves to **deny**, never allow.

`auto_approve = true` is intended for dedicated, single-user sandboxes. Treat it as "no human in the
loop" and compensate with `allowed_commands`.

## Shell hardening

* Default deny patterns always include `rm -rf /`, `del /f /s /q c:\`, `format c:`, `mkfs`,
  fork-bomb syntax, `shutdown`, `reboot`.
* `allowed_commands` matches the executable's base name, so `"/usr/bin/git"` and `python.exe` work as
  expected. This is a coarse filter — it does not stop `python -c "…"` from doing anything.
* Environment variables containing `API_KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `CREDENTIAL` or `AUTH`
  are removed before the child process starts.
* Output is capped (default 20 000 chars) and decoded with UTF-8 → locale → GBK fallbacks.
* Timeouts call `taskkill /F /T` on Windows or terminate the process group on POSIX.
* Commands run with `cwd` inside the workspace.

## Web exposure

Local-only defaults (`127.0.0.1`). If you bind `0.0.0.0`:

1. Set `security.web_auth_token` and pass `Authorization: Bearer <token>`; the static UI cannot send
   headers on its own, so prefer a reverse proxy with auth in front of it.
2. Put TLS in front (Caddy/nginx) — SSE works over HTTP/2 and HTTP/1.1.
3. Enable `enforce_sender_whitelist` for any messaging channel you expose.
4. Consider `tool_policy = "deny"` for tools you never want remote users to reach.

The web console has no user accounts or multi-tenancy: one process serves one operator.

## Webhook channels

QQ, Feishu and DingTalk endpoints live under `/api/channels/<name>/webhook`.

* Feishu: `verification_token` is compared with `header.token`. Encrypted payloads are rejected with
  a clear error instead of being processed.
* QQ: optional `webhook_secret` compared against a `token` field. Full Ed25519 signature
  verification is expected from a reverse proxy / bot gateway in front of Anyaclaw.
* DingTalk: outbound via group-robot webhook; inbound requires the optional `dingtalk-stream` SDK.
* All of them publish into the normal pipeline, so `exec` still requires approval (and is denied by
  default on bot channels).

## Prompt-injection considerations

Tool output (web pages, files, emails) is untrusted input. Mitigations:

* Tools return bounded, structured results; the model is instructed to cite URLs and never to paste
  file dumps.
* Dangerous tools cannot be triggered without policy approval.
* The workspace boundary means "read this file" cannot pivot into `~/.ssh`.
* Logs keep `trace_id` so an unexpected tool chain can be audited afterwards.

There is **no** defence against a user who deliberately approves a malicious command. That is the
human's decision, by design.

## Reporting

Please open a private advisory or email the maintainer for vulnerabilities. Do not open a public
issue for an unpatched sandbox escape.
