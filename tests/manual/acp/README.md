# Manual ACP probes (docs/design/acp-agent-backend.md §8)

Not run by pytest. Each script spawns `opencode acp` over stdio, speaks the
handful of ACP methods the design uses, and prints every frame. They produced
the `[live 2.2.6]` evidence in the design's Appendix A. Run them with
`python3 -I` from any directory. They need only the standard library.

| Script | Role |
|---|---|
| `probe_new_session.py <cwd> ["<prompt>"] ['<json env>']` | `initialize` → `session/new`. With a prompt: `session/prompt`, answering every `session/request_permission` with `allow_once`. The JSON env is merged into the process environment, for `OPENCODE_CONFIG_CONTENT`. |
| `probe_resume.py <cwd> <session id> "<prompt>" [session/resume\|session/load]` | `initialize` → `session/resume` (or `session/load`) of an existing session in a fresh process, then one prompt. Counts the replayed updates. |

The §4 force-ask ruleset, as the env argument:

```
'{"OPENCODE_CONFIG_CONTENT":"{\"permissions\":[{\"action\":\"shell\",\"resource\":\"*\",\"effect\":\"ask\"},{\"action\":\"edit\",\"resource\":\"*\",\"effect\":\"ask\"},{\"action\":\"external_directory\",\"resource\":\"*\",\"effect\":\"ask\"}]}"}'
```

Never answer `always` from these probes. opencode stores it as a project
allow rule in the shared SQLite database (design §3.6).
