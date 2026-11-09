# Files

- [Integration — Notion Direct REST Publish](notion.md) - How the engine creates Notion summary subpages via the direct REST API — parent ID extraction, Markdown-to-blocks mapping, chunking limits, Slide Descriptions child page, secrets handling, and idempotency.
- [Integration — OpenRouter HTTP Seam](openrouter.md) - Shared OpenRouter chat and transcription HTTP seams, per-stage model selection, env-var key handling, and retry and timeout behavior.
- [S3 Sync Integration](s3.md) - How the transcriber's gated S3 sync stage uploads a completed recording directory to an S3 bucket via the aws CLI, including config gating, pre-flight aws requirement, execution timeout, and manifest-based idempotency.
- [Integration — Telegram Dissemination and Topic Routing](telegram.md) - How Telegram summaries are gated, routed per topic, stripped of Markdown, chunked, and retried safely via the state manifest.
