# Manual ACP probes (docs/design/acp-agent-backend.md §8)

Not run by pytest. Each script spawns `opencode acp` over stdio, speaks the
handful of ACP methods the design uses, and prints every frame. They produced
the `[live 2.2.6]` evidence in the design's Appendix A. Run them with
`python3 -I` from any directory. They need only the standard library.

| Script | Role |
|---|---|
| `probe_new_session.py <cwd> ["<prompt>"] ['<json env>']` | `initialize` → `session/new`. With a prompt: `session/prompt`, answering every `session/request_permission` with `allow_once`. The JSON env is merged into the process environment, for `OPENCODE_CONFIG_CONTENT`. |
| `probe_resume.py <cwd> <session id> "<prompt>" [session/resume\|session/load]` | `initialize` → `session/resume` (or `session/load`) of an existing session in a fresh process, then one prompt. Counts the replayed updates. |
| `probe_agent_mode.py <cwd> <config path> [provider/model]` | The §4 P5 chain. Writes `<config path>` with per-watcher `agents.*` entries and spawns with `OPENCODE_CONFIG=<config path>` plus the force-ask ruleset in `OPENCODE_CONFIG_CONTENT`. Selects entries through the `mode` config option, adds and rewrites entries while the process runs, removes one under a live session, then resumes in a fresh process. Each step prints its result. The model defaults to `openai/gpt-5.6-luna-fast`. |

The §4 force-ask ruleset, as the env argument:

```
'{"OPENCODE_CONFIG_CONTENT":"{\"permissions\":[{\"action\":\"shell\",\"resource\":\"*\",\"effect\":\"ask\"},{\"action\":\"edit\",\"resource\":\"*\",\"effect\":\"ask\"},{\"action\":\"external_directory\",\"resource\":\"*\",\"effect\":\"ask\"}]}"}'
```

Never answer `always` from these probes. opencode stores it as a project
allow rule in the shared SQLite database (design §3.6).
