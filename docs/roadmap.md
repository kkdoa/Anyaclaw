# Roadmap

Ordered by what would most improve the project for new users.

## 0.2 — desktop + richer inputs

- [ ] Desktop shell package (`anyaclaw-desktop`) using the documented runtime contract; tray icon,
      sessions sidebar, approval dialogs. No runtime changes required.
- [ ] Image input: encode attached images for vision-capable models; OCR fallback for text-only ones.
- [ ] Streaming uploads and progress for large documents.

## 0.3 — memory and retrieval

- [ ] Optional semantic memory: embeddings + LanceDB/sqlite-vec behind a `MemoryBackend` interface.
- [ ] Per-skill retrieval: load skill bodies on demand instead of a summary line.
- [ ] Conversation search (`/api/search?q=`) over SQLite FTS.

## 0.4 — cost and safety controls

- [ ] Per-session and per-day token budgets with a graceful "budget exhausted" reply.
- [ ] Cost estimation table per model (`input_usd_per_mtok`, `output_usd_per_mtok`).
- [ ] Dry-run mode for mutating tools (show what would happen, execute nothing).

## 0.5 — platform coverage

- [ ] WeCom (企业微信) and Slack adapters.
- [ ] Discord adapter.
- [ ] Feishu encrypted events + file upload API; QQ Ed25519 webhook verification built in.

## 0.6 — extensibility

- [ ] Entry-point plugin discovery (`anyaclaw.tools` group), so third-party tools install as packages.
- [ ] Per-tool permission manifest consumed by `ToolPolicy`.
- [ ] Hot reload of `skills/` and config on file change.

## Always

- More tests for every bug fixed.
- Keep the core dependency surface at `httpx` + stdlib.
- Keep the single-process, single-operator deployment story simple.

Want something else? Open an issue with the use case.
