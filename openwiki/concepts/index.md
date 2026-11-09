# Files

- [Backend Selection (Slides and Summarize)](backend-selection.md) - How describe_slides and summarize select their openrouter and agno backends from config, why only those values are valid, and how unknown values fail without fallback.
- [Configuration model](config-model.md) - The transcriber's validated Config dataclass loaded from JSON or YAML, nested stages.slides, the mandatory openrouter block, backend selectors, Notion/Telegram/S3 fields, per-stage timeouts, and the secrets-by-env-name rule resolved at use time.
