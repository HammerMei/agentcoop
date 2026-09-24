# Manual lab drivers for coop-keeper (design §7)

Not run by pytest. These drove the §7 scenarios headlessly against the lab
Mattermost and Rocket.Chat for PR ②; keep them for the next round.

| Script | Role |
|---|---|
| `oc_turn.sh <session\|new> <name> "<prompt>" [timeout]` | One `opencode run` turn in the keeper directory, JSON events summarised by `oc_summ.py`; retries an attempt that never logs `init count=` within 40 s. |
| `cl_turn.sh <session\|new> <name> "<prompt>" [timeout]` | The same with `claude -p --output-format stream-json`. |
| `fill_profile.py <lab-profile> <keeper-profile>` | The operator's "fill in the credentials" step, copied field by field from the lab profiles file; prints only the field names. |
| `mm_dm.py <bot> "<text>" [wait]`, `rc_post.py <#room\|@user> "<text>" [wait]` | A human stand-in: post as the lab admin, print the bot's reply. |
| `mm_admin.py teams \| ensure-team <name> \| user <username>` | Lab housekeeping on Mattermost. |

Environment: `COOP_LAB_ADMIN_PROFILES` — a coop-provision profiles file holding the
lab servers' admin credentials, **not** the `~/.agentcoop/admin-profiles.yaml` the keeper
under test writes (default `~/.agentcoop/admin-profiles.lab.yaml`); `COOP_LAB_MM_PROFILE` /
`COOP_LAB_RC_PROFILE` (default `mm` / `rc`); `COOP_LAB_DIR` for logs (default `./lab-runs`);
`COOP_LAB_OPENCODE_MODEL` (default `opencode/big-pickle`); `COOP_LAB_KEEPER_DIR`.

Run the Python drivers with `uv run python tests/manual/<script>` (they need PyYAML).
Record each scenario as it happens — command, observed, pass/fail, deviation — the
table in PR #184's body is the shape.
