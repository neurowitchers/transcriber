# Files

- [Slide Description Workflow](slides-description.md) - How describe_slides builds slide inputs from extracted JPEGs and a scenes CSV, runs the openrouter vision backend one call per slide, writes <name>.slides.md, and is gated by the state manifest with ceiling and idempotency guards.
- [Summarize and Publish Workflow](summarize-publish.md) - How the summarize stage runs on the agy or agno backend, how slide markdown is fed to the summary prompt with fenced prompt-injection defense, the backend-specific Notion publish paths (agy MCP vs agno direct REST), the digest and .telegram.md artifacts, and the manifest gating that couples summarize+notion as one stage pair.
- [Transcription Pipeline](transcription-pipeline.md)
