# Triage Labels

The engineering skills speak in terms of five canonical triage roles. This table maps each
role to the label string used in this repo's GitHub Issues.

| Canonical role    | Label in this repo | Meaning                                  |
| ----------------- | ------------------ | ---------------------------------------- |
| `needs-triage`    | `needs-triage`     | Maintainer needs to evaluate this issue  |
| `needs-info`      | `needs-info`       | Waiting on reporter for more information |
| `ready-for-agent` | `ready-for-agent`  | Fully specified, ready for an AFK agent  |
| `ready-for-human` | `ready-for-human`  | Requires human implementation            |
| `wontfix`         | `wontfix`          | Will not be actioned                     |

All five already exist in the repo, and the label string equals the role name in every case.
When a skill names a role ("apply the AFK-ready triage label"), use the string from the
middle column — do not create a near-duplicate.
