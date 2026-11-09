# Files

- [Slide Description Workflow](slides-description.md) - How describe_slides extracts slides via scenedetect, enforces the max_slides cost guard, and runs one image-only OpenRouter vision call per slide under manifest gating.
- [Summarize, Digest, Routing, and Notion Publish](summarize-publish.md) - How the single agno-named summarize backend builds fenced prompts, calls OpenRouter directly, writes summary digest and slides outputs, partitions Telegram routing digests, and publishes atomically to Notion REST.
- [Workflow — Deterministic Transcription Pipeline](transcription-pipeline.md) - Deterministic media stage that converts a source MP4 into a plain-text transcript using ffmpeg audio extract, optional scenedetect slide extraction, and chunked OpenRouter STT with stitching and idempotent retries.
