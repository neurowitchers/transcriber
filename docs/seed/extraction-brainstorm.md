> **SUPERSEDED.** This is the original ad-hoc brainstorm (which proposed a
> pure-PowerShell approach). It is kept for history only. The current,
> authoritative seed is **[`transcriber-engine.md`](./transcriber-engine.md)**,
> which supersedes the direction below (the engine is implemented in Python +
> `uv`, adopted by host repos as a git submodule).

---

# How to combine two ad-hoc setups in a real app

There are two set-ups that I use to transcribe meeting video recordings locals. Both are run as local agent automation (by Orca, but this may not be relevant)

- First, more advanced, see `../../scartill/scartill-ai-hub`.
- Second, less advanced, see `../../adsight/ai-hub`

Task:
- generalize the basis of both into this application (it can be then attached as e.g. a git submodule)
- preserve customizations
- re-do into a pure pwsh scripts, which can call agents headless
- remove intermediate files when done
