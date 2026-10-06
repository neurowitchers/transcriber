# Files

- [Backend selection and interfaces](backend-selection.md) - How the transcriber selects describe_slides and summarize backends as pure functions of config, the concrete slides/openrouter and summarize/agno backends, their shared OpenRouter block with per-stage models, and the Notion publish split.
- [Configuration model](config-model.md) - The transcriber's validated Config dataclass loaded from JSON or YAML, nested stages.slides, the mandatory openrouter block, backend selectors, Notion/Telegram/S3 fields, per-stage timeouts, and the secrets-by-env-name rule resolved at use time.
