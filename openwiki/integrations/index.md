# Files

- [Notion integration](notion.md) - Two Notion publish paths in the transcriber — MCP-based publishing for the agy summarize backend and direct REST publishing for the agno backend — plus config fields, token_env handling, and insertion mode.
- [OpenRouter Integration](openrouter.md) - Shared OpenRouter HTTP seam, OpenAI-format transcription and vision calls, per-stage model selection, env-var-based API key handling, and retry/timeout behavior across the transcriber's paid stages.
- [S3 Sync Integration](s3.md) - How the transcriber's gated S3 sync stage uploads a completed recording directory to an S3 bucket via the aws CLI, including config gating, pre-flight aws requirement, execution timeout, and manifest-based idempotency.
- [Telegram Integration](telegram.md) - How the transcriber sends meeting summaries to Telegram — bot token resolution, topic routing, Markdown stripping, message chunking, digest vs full-summary preference, and manifest-gated sends.
