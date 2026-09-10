## Rocket.Chat Room Profiles

**IMPORTANT — scope:** The profiles below apply **only** when interacting via the Rocket.Chat
gateway (i.e., when the `[Rocket.Chat #<room> | from: <username> | role: ...]` message prefix
is present). Do NOT apply these profiles during CLI/terminal sessions.

Cross-reference the `from: <username>` field in the message prefix with the profiles below to
personalize your tone, language, and response style for each person in the room.

---

### alice
- **Display name:** Alice
- **Title:** Engineering Lead
- **Language:** English
- **Notes:** Prefers concise technical answers. Comfortable with code snippets. Appreciates
  bullet points over paragraphs. Often asks about system internals.

### bob
- **Display name:** Bob
- **Title:** Product Manager
- **Language:** English
- **Notes:** Non-technical background — avoid jargon; use plain language and analogies.
  Focuses on business impact and timelines, not implementation details.

### charlie
- **Display name:** Charlie
- **Title:** Contractor (Guest)
- **Language:** English / Traditional Chinese (replies in whichever language Charlie writes in)
- **Notes:** Has read-only access (guest role). Primarily asks questions about documentation
  and project status. Keep responses factual; do not share internal system details.

---

## How to Use This File

1. Copy this file to your project context directory:
   ```bash
   cp contexts/rc-room-profiles.example.md contexts/rc-room-profiles.md
   ```

2. Replace the example profiles with the real people in your room.

3. Reference the files in your `config.yaml`. `rc-gateway-context.md` belongs at the
   **connector level** (shared across all rooms); room profiles are **watcher-level**
   (specific to each room):
   ```yaml
   connectors:
     - name: rc-main
       ...
       context_inject_files:
         - contexts/rc-gateway-context.md   # Gateway behavior rules — shared across all rooms

   watchers:
     - name: general
       connector: rc-main
       room: general
       agent: claude
       context_inject_files:
         - contexts/rc-room-profiles.md     # Room member profiles (this file)
   ```

4. Restart the gateway or reset the watcher to load the new context:
   ```bash
   coop reset general
   ```

---

## Profile Fields (all optional)

| Field | Description |
|-------|-------------|
| `Display name` | Friendly name to address the person by |
| `Title` | Role or job title for context |
| `Language` | Preferred language(s) for responses |
| `Notes` | Tone, style, or domain hints for the agent |

Keep profiles concise — the agent reads this on every new session. Long profiles increase
token usage without much benefit; a few targeted bullet points per person is ideal.
