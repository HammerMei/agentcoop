# Prior art: agents as members of human chat rooms

Date: 2026-11-16. Status: research notes for a direction review. Nothing here is a design decision.

## 0. Scope and method

**Yardstick use case.** Agents that each bring their own brain (LLM), memory, skills and harness, owned by different people and not necessarily provisioned by the platform, join a human chat room as members, so people and agents work together in one open-ended conversation (a meeting, a brainstorm, social talk, an ad-hoc task) on whatever that room is for.

**Two stage-1 filters**, each decided by one quoted line from the project's own material:

1. Is the unit of work an open conversation rather than a task flow?
2. Can an agent the platform did not provision, owned by someone else, join as itself?

A project passes both → SAME. Passes one or is unverifiable → ADJACENT. Fails (fixed flow, single owner, single vendor by design) → EXCLUDED.

**Stage 2** deep-dives the SAME projects on the columns that discriminate:

- join prerequisites: what the agent must be able to do (shell, remote MCP, plain HTTP, nothing)
- wake path: when a human posts and the agent is not running, who starts its turn
- identity and ownership: one account per agent, can owners differ, what the owner controls
- who speaks: turn-taking and loop handling in the project's own terms
- humans in the same room, and which chat surface
- open source, self-hosting, maturity

**Evidence tags.** `[docs]` official docs, README, changelog or spec; `[src]` code read; `[marketing]` landing, feature, FAQ or vendor blog page; `[third-party]` press or another vendor; `[snippet]` a search-engine excerpt of a page that could not be fetched. "not stated" means the pages read do not say it; nothing was inferred to fill a cell. Quotes were returned through a summarising fetch tool and were not re-verified character by character except where marked "verified".

**Caveats.**

- Meta Help Center pages render only a title to the fetcher; Muse quotes from those pages are `[docs, via snippet]`.
- Grok Bot's docs at docs.x.ai/grok-bot describe a Cursor-integrated product; reported as written.
- Instinct's product site is instinct.com; instinct.ai is a parked domain. No docs, FAQ or developer page exists; the blog returned HTTP 500.
- AgentProvision's site is JavaScript-rendered; every route returned the title only.
- Bloome has no docs, help or setup pages (sitemap: /features, /guides, /blog only).

Three claims the conclusions rest on were re-read at source: Ando's membership and receive methods (docs.ando.so/developers/running-agents.md), Agent Room's network door (agentroom.chat/agents.txt), Grok Bot's routine triggers (docs.x.ai/grok-bot/skills-routines-and-automations). Marked "verified" below.

---

## 1. Stage-1 classification

| Verdict | Project | Deciding line (project's own words) | Tag |
|---|---|---|---|
| SAME | Ando | "Ando is agent-agnostic." / "An agent that connects to Ando is a workspace member." | [marketing FAQ] / [docs] verified |
| SAME | Agent Room | "They can bring their own agents, and every agent shows whose it is." | [docs README] |
| SAME | Aurmada Rooms (seat and skill.md surfaces) | "Each person brings the AI they already use" | [marketing] |
| SAME | Conduyt Team Chat | "Claude Code, Codex and Grok Bot can sit side by side in the same Conduyt channel, each owned by a person on your team." | [vendor page] |
| SAME | Buzz (block/buzz) | an agent is a channel member with its own keypair, bridged to an ACP harness | [src], from the design document's Appendix D |
| ADJACENT | Bloome | "Humans and AI agents share one conversation — same threads, same @mentions." but every mechanism cell is not stated | [marketing] |
| ADJACENT | Moltbook | "A Social Network for AI Agents." / "Humans welcome to observe" | [marketing] |
| ADJACENT | AgentChat (clawhub skill) | "The messaging platform for AI agents"; all participants are agents | [third-party] |
| ADJACENT | Instinct group chats | own group agent inside its texting thread; non-Instinct friends can be in the group, but the agents are Instinct's | [third-party] |
| ADJACENT | Grok Bot in-app group chats | 2–6 Bots, all Grok Bots | [docs] |
| ADJACENT | Aurmada surface (c): A2A switchboard / paid jobs | "One endpoint to reach, pay and trust any AI agent." — tasks with artifacts, not a conversation | [docs] |
| ADJACENT | Slack | "Agents are autonomous, goal-oriented AI apps…" — a developer-built app installed to a workspace | [docs] |
| ADJACENT | Microsoft Teams | "Teams SDK is the developer toolkit for building conversational agents…" — developer-built app | [docs] |
| ADJACENT | Google Chat | "The Chat API lets you build Google Chat apps…" — developer-built app | [docs] |
| ADJACENT | Mattermost Agents | admin-configured LLM bots; "Some agents are configured by an admin to use service account authentication" | [docs] |
| ADJACENT | Rocket.Chat | MCP server: "All operations are performed on behalf of an authenticated user" — an external agent acts as a human's token | [docs] |
| ADJACENT | Shogo | own harness supplies the agents; bring-your-own agent not stated; "There's no one-command quickstart yet." | [docs] |
| ADJACENT | Dust | "Use @ to mention both agents and teammates in a conversation." — workspace-built agents only | [docs] |
| ADJACENT | Taskade | "AI agents and team members can be in the same chat." — workspace-built agents only | [docs] |
| ADJACENT | Tokenrip, Nimbalyst, Notion, monday.com | shared workspace (files, tables, tasks), not a conversation; MCP connections | [marketing] |
| UNCLASSIFIABLE | AgentProvision | title only: "One room for your people and your AI team"; search snippets suggest platform-run agents on a chosen engine | [marketing], [snippet] |
| EXCLUDED | Glean | "Auto mode, Workflow mode, templates, the headless builder" with "triggers, steps, tools, flow" — fixed flow | [docs] |
| EXCLUDED | CrewAI | "Tasks are executed one after another, allowing for a linear flow of work." | [docs] |
| EXCLUDED | AutoGen, Google ADK, MetaGPT | in-process group chat or task pipelines (Appendix D) | [src] |
| EXCLUDED | OpenBot | one host owns every member; task tree; one bot identity per platform; its author calls three agents on one ambiguous task an anti-pattern (Appendix D) | [src] |
| EXCLUDED | munder-difflin | hub design: a "god" agent decomposes work and is the sole writer of the shared board (Appendix D) | [src] |
| EXCLUDED | jinn | org chart of AI employees, single owner (Appendix D) | [src] |
| EXCLUDED | QM | one company bot shared by many humans (Appendix D) | [src] |
| n/a | OpenClaw, Hermes | these are agents, not rooms; covered in §6 as candidates that join rooms | |

---

## 2. Same-use-case projects

### 2.1 Ando (ando.so, docs.ando.so)

| Row | Finding | Tag |
|---|---|---|
| Classification | SAME. "Ando is team messaging built for humans and agents to work together."; agents "manifest just liks human members in the workspace." (sic). "An external agent runs outside Ando, on infrastructure you or its maker operate." "An external agent is one you already run" and it "joins your Ando workspace as its own member." | [marketing blog], [marketing learn], [docs external-agents] |
| Join prerequisites | In the invite dialog choose Agent, copy a one-time link: "Each link connects one agent directly and expires after 1 hour." The agent "verifies its identity and workspace, then introduces itself". Custom agent: follows the link, POSTs the passcode, stores the API key, then uses MCP Streamable HTTP at `https://mcp.ando.so/mcp` with `Authorization: Bearer <key>`, or REST with `x-api-key`; must reach `agents.ando.so` and `mcp.ando.so`; Ando hides it until it answers a "Send check" in the inviter's DM. OAuth clients choose "Add as an agent" vs "Connect as me". The agent must also run a receive path (see wake path). | [docs external-agents, another-agent] |
| Contradiction on Claude/Codex | Marketing: "An external agent is one you already run, such as Claude, Codex or Grok Bot, invited into the workspace." Docs: "Claude, ChatGPT and Codex don't join Ando as agent members."; "Codex is not a workspace member"; verified in running-agents.md: "A person's Claude Code or Codex connected from the desktop app or a server connector is not an agent" and "never becomes a member". They connect "as you" or as a local tool connection. | [marketing], [docs] verified |
| Non-coding assistant as itself | Only OpenAI dots: "A ChatGPT dot is OpenAI's agent, running on its own cloud computer." Invited with an open Dots invite; needs the ChatGPT workspace "Cloud network access" setting ("Without it, the dot can't reach Ando."). claude.ai: "A Claude chat runs only while you're in it." and "nothing in Ando can wake it."; "To have Claude answer messages while you're away" add an Ando-run agent. | [docs openai-dots, claude-chatgpt-and-codex] |
| Wake path | Verified: "MCP tools and normal HTTP requests are pull-based unless a modern MCP client creates an event subscription." "Choose one receive method:" MCP Events (`events/subscribe`, `agent.wake`; protocol revision 2026-07-28), HTTPS receiver (signed webhook), WebSocket (`ando.realtime.v1`: "Receive message events or wake a local agent when its inbox changes."). "Polling and outbound WebSockets need no public inbound port." Scheduled inbox check (`get_agent_inbox`) is the fallback for OpenClaw and dots: "hears new messages only when a scheduled check of its inbox runs." FAQ: "MCP gives a running agent tools, but it doesn't wake an idle one." | [docs running-agents, realtime] verified, [marketing FAQ] |
| Notify / wake policy | Verified values: default `mentions_and_direct_messages`; "`mentions_only` narrows that to direct mentions"; "`all_messages` widens it to every message in the agent's chats"; "`disabled` delivers nothing until an admin changes". Docs API calls it `effective_wake_policy`, "the agent's Notify ceiling". The features page lists four options; docs/agents lists three (Off, Mentions, All messages) — inconsistent. | [docs] verified, [marketing] |
| Identity and ownership | "An agent that connects to Ando is a workspace member." "Everything it does is attributed to the agent, not to you." Own name, profile, inbox. Any full member can add an agent ("Guests can only connect as themselves."). "Workspace owners and admins can change any agent's access. The person who connected the agent can change that agent's access." Access levels: "everything an agent can be allowed, including channels, invites, automations and connected apps." / "read, reply and work through its inbox and tasks without changing the workspace's setup." / "read what it can see but not post, upload or change anything." For Ando-run agents: "you decide which app connections and tools it can use." Billing: "Agents are unlimited on every plan and never take a seat." Cross-org: "agents from the other organization count as context, not as triggers for yours." | [docs], [marketing] |
| Who speaks | Notify ceiling plus mentions: "An agent can mention another agent in a conversation they can both access."; agent-authored messages wake another agent "when the target agent can access the chat and its Notify setting allows the message". "Ando suppresses an external agent's own authored events."; the runtime "should deduplicate events and avoid replying just to continue an agent-to-agent chain."; agents wake each other "within limits that prevent loops" (numbers not stated). Ando-run agents have a "participation judge" deciding whether "Relevant messages" reach an agent; each Ando agent decides to ignore, react with one emoji, or start a turn; per-channel proactivity level Mentions/Low/Medium/High. Blog: agents can "pick up work without being explicitly tagged". | [docs running-agents, agents], [marketing] |
| Humans and surface | Yes. Ando's own Slack-like app (channels, DMs, threads, group DMs); desktop app for Mac/Windows/Linux; "Both are members and can work in the same chat." | [marketing] |
| Open source / maturity | Not open source (not stated); "can't be self-hosted". Hosted SaaS, "Request access", "New workspaces start with a 7-day trial." Plans: Free "$0forever", Pro "$7.25per person a month", Business "$15per person a month" (yearly). Blog: "We raised $20M. Ando is out of stealth."; Index Ventures led the Seed (post dated 2026-09-24); founder Sara Du. Public API spec dated 2026-11-16. | [marketing], [third-party], [docs] |

What Ando optimises for, in its own terms: agents as first-class workspace members, with the receive path as the owner's responsibility. The platform guarantees identity, attribution, access levels and a wake policy; the owner runs a WebSocket, webhook or MCP-Events listener, or polls. Ando's own hosted harness exists for teams without one. The practical gap against the yardstick: the headline "external agents" (Claude, Codex) are, per the docs, tools or "as you" connections, not members; only a self-hosted runtime or an OpenAI dot becomes a member in its own right.

### 2.2 Agent Room (agentroom.chat, github.com/rainyflash/agent-room)

| Row | Finding | Tag |
|---|---|---|
| Classification | SAME. "open-source chat room where people and AI agents talk to each other"; "They can bring their own agents, and every agent shows whose it is."; "Bring any AI agent into your room with one message." | [docs llms.txt, README] |
| Join prerequisites | Three doors in the README: "Network suits any agent that can reach the internet." / "MCP gives one generic configuration for any MCP-capable agent tool on this computer." / "Command line is for agent tasks that can run local commands." Flow: human signs in at app.agentroom.chat, creates a room, presses "Bring an agent", picks a door, copies the generated message and pastes it into the agent. CLI and local-MCP doors need the desktop app (Bridge): "credentials and device keys stay on your computer". | [docs README, guide], [marketing] |
| Network door (verified in agents.txt) | `POST https://api.agentroom.chat/v1/network-agents` with `{"name": "…"}` returns a bearer token (returned once; not to be pasted into chat). `GET /v1/network-agents/me/messages?wait=30` — loop "wait, handle, acknowledge, wait again at once", including after empty returns. `POST /v1/network-agents/me/ack` with the last handled `eventId`. `POST /v1/network-agents/me/messages` with `{"text", "submissionId": "<UUIDv7>"}`. "只有在等的时候你才收得到消息：回完一句就不等了" (you receive only while waiting). Unacknowledged messages stay in the inbox, up to 500 per room. agents.txt names two paths (HTTPS and MCP) and says the HTTPS path needs no app, no CLI and no account; the command-line door is in the README/guide, not in agents.txt. Browse-only agents cannot join directly: the owner adds the MCP connector and the agent calls `agent_room_join`. | [docs agents.txt] verified |
| Remote MCP | `https://api.agentroom.chat/mcp` (Streamable HTTP), tools `agent_room_join`, `agent_room_wait_for_messages`, `agent_room_ack`, `agent_room_send_message`; bearer header if the host can set headers, else `token` per tool. ChatGPT guide: add the connector, no sign-in; "some apps only offer custom connectors on paid plans or in a developer mode". Landing: network path "reads one guide and comes in, with nothing to install". | [docs agents.txt, guide], [marketing] |
| Wake path | Pull by the agent; nothing wakes a stopped agent. README: "Agent Room never pushes remote text into a local agent's context." Local MCP README: "MCP 工具本身不会唤醒已结束的任务。" architecture.md: "不能唤醒关闭的宿主". Running agents long-poll (30 s per HTTP call; local MCP can block longer). Unattended replying needs "Room settings → Automation", desktop-connected agents only; "An agent you've allowed to answer replies in the room." ChatGPT via connector "can't stay in the room listening"; messages wait until the user asks it to check again. | [docs README, MCP README, architecture.md, guide], [marketing] |
| Identity and ownership | "one host task is one agent: it can be in several rooms at once". Owners differ in a room ("every agent shows whose it is"); network agents are marked and show their owner. ChatGPT-style agent identity "lives in that conversation" (new chat = new agent). Automation permission "covers one room, expires, and caps how many messages the agent may send."; room managers admit/refuse knocks and remove agents. One Matrix account per agent: not stated (architecture.md speaks of a per-agent device identity in the Bridge). | [docs] |
| Who speaks | "By default an agent wakes only for messages that concern it and waits until the conversation pauses". Triggers: anything a person says (unless addressed to someone else), agent messages only when they mention/reply, and anything its owner says; "Two agents can't keep each other busy by accident." Settle: wait for a 5 s pause, typing counts as talking, capped at 30 s. Caps: public lobby 20 msgs/min/agent, 12/min lobby-wide; Automation message cap. No classifier. | [docs README, guide, MCP README, agents.txt] |
| Humans and surface | Yes. Own web app and desktop app (Tauri), built on Matrix: "Matrix/Synapse carries rooms, membership, timelines, devices, E2EE, and federation." "Everything it says stays visible, and you can take over at any time." | [docs README], [marketing] |
| Open source / maturity | MIT; self-hostable with Docker Compose (Synapse, Rust control plane, Keycloak, Postgres, S3). Repo created 2026-08-25, pushed 2026-11-17, 21 stars, 0 forks, Rust. v0.1.0-alpha.94 published 2026-11-10; "is signed but still an alpha: expect rough edges and frequent updates." Self-hosting doc: "A clean public-host acceptance run has not yet completed." | [docs self-hosting.md, README], [GitHub API] |

What Agent Room optimises for: the agent owner's safety boundary. The agent decides when to read, keys stay on the owner's machine for local doors, and unattended replying is a per-room, expiring, message-capped grant. The three doors are sized to agent capability (shell / MCP tool / plain HTTPS); the network door is the lowest bar found in this set. Contradiction to note: the landing's "Everything it says stays visible" coexists with private rooms whose keys the server holds for network agents (llms.txt).

### 2.3 Rooms by Aurmada (rooms.aurmada.com)

Three surfaces: (a) a person's "AI seat" via MCP connector; (b) a `skill.md` heartbeat agent via REST; (c) an A2A switchboard / jobs / x402 paid calls. (a) and (b) are SAME; (c) is task-oriented and excluded.

| Row | Finding | Tag |
|---|---|---|
| Classification | "Your team and its AIs. One shared room." / "Two people. Three AIs. One thread." "Each person brings the AI they already use". Event: "Bring your own AI — Claude, Cursor, ChatGPT, OpenClaw or your own open-source agent — into one shared, multiplayer Room." Surface (c): "One endpoint to reach, pay and trust any AI agent." | [marketing], [marketing luma], [docs llms.txt] |
| Join prerequisites | (a) Person joins "with just a name, then connect their own AI"; paste the MCP link "as a connector in ChatGPT, Claude, Gemini or Cursor and sign in."; per-AI MCP link and scoped token from "Your AI keys". (b) skill.md: POST agents/register → API key (once) + claim link; human claims via X/GitHub/email; unclaimed agents limited to 1 post / 5 min and expire in 7 days; then a heartbeat loop: GET `/api/v1/home`, answer `pending_mentions`, post with `Idempotency-Key`. Needs only HTTPS. (c) A2A 1.0 JSON-RPC at `/a2a/v1` (SendMessage/GetTask/CancelTask). Non-coding assistant: yes via (a), any MCP-capable chat app, as an "AI seat". | [marketing welcome/lobby], [docs skill.md, llms.txt] |
| Wake path | (a) By client capability (changelog): v0.33.0 (2026-10-16) "AI apps that support MCP Events, starting with ChatGPT in Work chats, can subscribe to rooms and be woken when @mentioned"; v0.35.0 (10-19) when a closed chat-app AI is @mentioned, Rooms posts a labelled stand-in answer, and "always-on AI seats" answer at once; v0.6.0 Room Host "nudges owners when an AI is asked something" with email/web-push; each asleep AI has a Wake button in the People panel (v0.35.1). (b) Pull on a schedule: `heartbeat_minutes: 180` in skill.json; llms.txt says 30–60 min — the two conflict. (c) Push: A2A SendMessage. | [docs changelog, skill.json, llms.txt] |
| Identity and ownership | One key/seat per AI per person; owners differ by design. skill.md agent: "Your human is accountable for you."; managed under Rooms → Account → Your agents. Owner controls what the AI sees: "consent-scoped shared context", capsules private until Share is on; keys revocable. Pay-per-call via x402 for surface (c) (post $0.002, read $0.001 USDC on Base). | [marketing], [docs llms-full.txt, /paid] |
| Who speaks | "Mention an AI and it answers." "AIs hand work to each other, and guardrails stop them looping." "Rooms caps AI-to-AI turns without a person and pauses AIs that repeat themselves." "A `429 ai_turn_cap` means wait for a person." skill.md: each AI max 3 msgs / 10 s, room max 6 AI posts / 10 s, default cap 20 AI turns without a human present; claimed agents 1 msg / 20 s / room, 200/day. Changelog v0.35.0/v0.35.2: loop guards end an exchange after a set number of turns "until a person speaks again"; per-AI and per-room burst limits. Heartbeat agents reply `NO_REPLY` when nothing to add. No classifier. | [marketing], [docs] |
| Humans and surface | Yes. Own web app; guests join a room or call by name; Google/email sign-in optional. Separate "People" and "AI agents" lists and an AI access log per room. | [marketing] |
| Open source / maturity | Open source: not stated (no license, no repo link; SDKs on npm/pip mentioned). Self-hosting: not stated. Hosted SaaS; changelog from v0.5.0 (2026-10-06) to v0.38.2 (2026-11-16); skill.json reports Rooms "2.0.0" / app 0.38.2. "free to start"; Lobby free; "Plans" link with no details read; enterprise via email. | [docs changelog, skill.json], [marketing] |

What Aurmada optimises for: vendor-neutral seats plus an agent marketplace. The room is a thread where each person's own AI sits with scoped context, and the same identity can take paid jobs or be reached by other agents over A2A/x402. Turn-taking is numeric caps (per-AI burst, per-room burst, AI turns without a human), not addressing rules. The product moved very fast in Oct–Nov 2026 and its docs disagree on heartbeat cadence.

### 2.4 Conduyt Team Chat (conduyt.com)

| Row | Finding | Tag |
|---|---|---|
| Classification | SAME. "Claude Code, Codex and Grok Bot can sit side by side in the same Conduyt channel, each owned by a person on your team." "Team Chat puts your people and their AI agents in the same channels, next to the deals." "Each agent stays on its owner's computer and acts with its owner's permissions." Each agent "runs on its owner's own AI subscription". | [vendor page], [marketing team-chat] |
| Join prerequisites | Owner: "In Settings > AI, create the teammate with the provider Claude Code and get a pair code." Then on "a computer that stays on": `npx -y conduyt@latest agent listen --adapter claude-code --url https://conduyt.app --pair-code <pair code>` in "a terminal that stays open"; "It answers only while this computer is awake and Terminal is open." Pair code: "single use and expires 15 minutes after you get it." "It is not a token." Codex: `--adapter codex --thread <thread id>` plus the Codex desktop app. The claude-code adapter runs one `claude --print` per turn in an empty temp dir with user/project settings, slash commands and built-in tools disabled; only Conduyt tools (reply, react, stay_silent, linked_record, read_thread, read_messages, search_messages). A generic `--adapter command --command ./agent.sh` adapter: prompt to stdin, stdout posted as the reply. | [docs connect page, CLI README] |
| Non-daemon route | Grok Bot "Runs with nothing on your computer." — Grok adds "the Conduyt connector" and "creates a routine called 'Conduyt Team Chat'"; the owner pastes the routine's "POST to" address and key into Conduyt. Whether any other MCP client can use that route: not stated. Stated limit: "a Cowork task cannot keep a process alive after it ends." | [docs connect page] |
| Wake path | Listener route: pull — "long-poll + heartbeat", server-owned cursor, heartbeat every 30 s. Offline: "A turn asked while your computer was off is still answered when the listener returns." "as long as it comes back within 2 hours." "A failed turn is not re-run for you." Connector route: Conduyt stores the routine's address and sends a "hello"; the turn payload it POSTs is not stated. MCP endpoint `POST /api/v1/chat/agents/me/mcp`, "stateless Streamable HTTP … JSON only, no session id" (dev.to post only). | [docs CLI README], [vendor blog] |
| Identity and ownership | "Every message an AI posts carries its own name, avatar and handle". "Each AI teammate should have its own session or account." Owners differ within a room ("each owned by a person on your team"). Owner = "whoever adds it in Settings, not the person who runs the AI." "The AI acts with the visibility of the person who created the teammate." "If you can't see a deal, neither can your AI." Sending as a person needs a "Needs a human OK card". 200 turns/day per teammate; per-key limits (120 requests/min; writes 500/hour). | [docs], [marketing], [vendor blog] |
| Who speaks | "An AI stays quiet unless it is addressed, answers once per turn." Triggers: "when someone mentions it, or when a person replies inside a thread where the AI gave the latest answer." "it does not reply to another AI's top-level post on its own." Agents may @mention each other. Cap: "lets six AI posts in a row stand in one thread, then pauses them." Pause line: "AI agents paused, this thread needs a human"; "Any person posting in the thread lets them continue." Per-channel admin kill switch "Agents respond here" ("A mention still reaches the AI but it will not answer."). History: on the dev.to test day the cap was 2, raised to 6 that night. Idempotency: `replyIdempotencyKey` / "Reply at most once per turn"; `turnToken` expires after 60 minutes; only messages at most 10 minutes old are delivered; invalid replies get HTTP 409 `turn_token_invalid` with "nothing was posted". MCP server instructions: answer with a reply, a reaction, or nothing. | [docs], [vendor blog] |
| Naming mismatch | The dev.to post's `turnToken` and `replyIdempotencyKey` do not appear by those names in the CLI README, which uses a claim token `<generation>:<listenerId>` (`--claim-token`) and `--idempotency-key reply:<sourceId>`; the connect page says "A turn also carries a short-lived turn token" that "lets your AI post one reply, in one thread, for up to an hour." Same object under two names: not stated. | [docs], [vendor blog] |
| Humans and surface | Yes, same channels/threads; Conduyt's own web app ("Team Chat" inside the Conduyt CRM). | [marketing] |
| Open source / maturity | CLI `conduyt` on npm, MIT, v1.91.x. Platform open source / self-hosting: not stated. "AI teammates are included on Professional and in your 20-day free trial." Claude Code how-to dated 2026-10-29; three-vendor post 2026-11-08 ("on our production app"). | [docs], [vendor blog] |

The dev.to experiment (three agents, three vendors, one thread, no human after the first prompt): Drafter (Claude), Reviewer (Grok), Finalizer (Codex) wrote a five-item QA checklist. Problems the author reports: each turn needed to show where it came from and where the reply should go; turns did not include a roster of channel members or a deadline (the Reviewer pointed it out; both planned); agents mentioning each other could talk indefinitely, so caps were added; an earlier cap ended a round-table before every agent had spoken; agents needed limits on what they could access. The thread paused on its own after 78 seconds. Advice: "start with the idempotency key and the pause line." Single test, author's own product.

What Conduyt optimises for: "Each mention gets one reply, never a stream of them."; "A person stays in control."; agents answer "with live CRM data" under "its owner's Conduyt permissions, never more." CRM-first; the chat is "next to the deals".

### 2.5 Buzz (block/buzz, from Appendix D of the design document)

Team chat on a Nostr relay where an agent is a channel member with its own keypair, bridged to an ACP harness. The mention gate applies to agents and humans alike, at the relay: an unmentioned post never reaches the agent's process (`crates/buzz-acp/src/relay.rs:3366-3372`). By default an agent accepts only its owner and the owner's other agents (`config.rs:93-105`). Agent-to-agent mention is a designed path (a delegate must mention the delegator when done); nothing in code bounds the exchange that follows. Loop control is prompt text ("silence is usually correct", "never publish a bare acknowledgement … it re-triggers everyone you mention", `base_prompt.md:71-75`); a controversial exchange between two agents runs until an owner cancels it. Context for an unaddressed agent is the thread's history fetched at dispatch when it is next mentioned in that thread, with already-delivered events removed (`pool.rs:4055-4073, 4136-4150`); a top-level channel post fetches nothing. No coordinator, plan, rate limit or mode; the failure notice carries no mention tag and cannot wake another agent (`pool.rs:5540-5548`). No test runs two agent identities in one channel. All `[src]`.

---

## 3. Adjacent projects

### 3.1 Bloome (bloome.im)

Claims the use case: "Humans and AI agents share one conversation — same threads, same @mentions." / "Bring Claude Code, Codex, Gemini CLI, or OpenCode into a chat via ACP." / "Bloome lets the flow emerge in conversation." / "Every agent has a profile and sits in the member list, just like people." `[marketing]`. External coding agents: "These are third-party integrations you connect yourself, not official plugins." Whether an agent owned by a different person can sit in another person's room: not stated.

- Join: "Sign up, open a group chat, and add Claude Code as an external agent through Bloome's agent connection." "Once connected, @mention it in any thread to start a task." What the user installs or runs, where the agent process runs: not stated. No setup/help pages exist. Non-coding path: only Bloome's own provisioned agent ("Sign up and a personal AI agent is created for you").
- Wake: not stated. Only "@mention an agent in any channel or thread and it replies in real time."
- Identity: "Every account gets a personal agent on sign-up." "The agent lives in a shared conversation, so any teammate in the chat can @mention it". One account per agent, differing owners, owner controls: not stated.
- Who speaks: mention/reply/thread-driven; "loop protection stops agents triggering each other endlessly" — mechanism and limit not stated; a blog "publish-boundary check" after which an agent may "revise or stay silent".
- Surface: web, macOS, Windows, iOS, Android. Open source / self-hosting: not stated. Free to start, credit billing. Launch reported 2026-06-30 `[third-party, testingcatalog]`; cloud sandbox "E2B sandbox (Beta)".
- The "ACP" conflict: Bloome expands the acronym four ways — "its own agent protocol (ACP), which you set up yourself"; "its agent connection protocol (ACP)"; "ACP (the Agent Client Protocol) as a third-party connection"; and a blog "Agent Collaboration Protocol" (2026-06-18, a design essay with no transport or setup). Whether the Claude Code/Codex link is the Zed Agent Client Protocol over a local process is not stated.

Optimises for: "a team of agents that delegate, share context, and cross-check each other in a single chat"; coordination through "ordinary chat — mentions, replies, and threads".

### 3.2 Moltbook (moltbook.com)

"A Social Network for AI Agents." / "Humans welcome to observe" `[marketing]`. Any agent registers itself via API and is claimed by its owner; posting, commenting and voting are "limit[ed] … to AI agents authenticated through their owner's 'claim' tweet" `[third-party, Wikipedia]`. Fails the yardstick on humans working in the room.

- Join `[docs skill.md]`: owner tells the agent to read `https://www.moltbook.com/skill.md`; agent POSTs `/api/v1/agents/register` with `name`, `description`; response carries `api_key`, `claim_url`, `verification_code`; the human verifies email and posts a tweet. Plain HTTP with a bearer key; no daemon, no shell.
- Wake: pull only; recommended "every 30 minutes" heartbeat — fetch `heartbeat.md`, `GET /api/v1/home`, reply/post.
- Identity: one API key per agent; one human owner via claim; owner dashboard can rotate the key.
- Who speaks: rate limits only — posts 1 per 30 min; comments 1 per 20 s, 50/day; new accounts tighter.
- Maturity `[third-party]`: launched 2026-01-28 by Matt Schlicht; Meta acquired it 2026-03-10; security incidents (open DB, exposed Supabase key). Platform open source: not stated.

### 3.3 AgentChat (clawhub "agentchat skill", an OpenClaw skill)

"The messaging platform for AI agents" — all participants are agents `[third-party]`. Groups up to 256 members, addressed by `grp_` conversation id; "every active member receives a copy on their next /sync"; mentions are "a cosmetic convention peers can parse, not a routing primitive"; polling-only via a HEARTBEAT.md task (1 min in its example; OpenClaw's current docs say the runtime no longer reads HEARTBEAT.md); at-least-once delivery, "your processing must be idempotent"; no turn-taking or floor control; "conversation, not RPC".

### 3.4 Instinct (instinct.com)

Official surface: "You can text or call it." "It can use a phone and computer in the same way that you do." Privacy policy mentions "information from applications and services you have connected" and Google OAuth. No docs, FAQ or developer page. Group chats (early access, announced 2026-10-05, `[third-party, TechCrunch]`): "A new Instinct joins and works for the whole group"; works even if friends have not joined Instinct; a personal agent asks permission before connecting with the group's agent and trust is revocable; the group agent is siloed from users' personal accounts; when new members join, pending replies from a personal agent are held before being shared; one source reports agents coordinated "300k+ times" in the first week. Messaging platform: not stated. A user-reported constraint (polls a channel only every 5 minutes, cannot hold a WebSocket, no API to wake it) is unverified: no public Instinct docs confirm or deny it.

### 3.5 Grok Bot in-app group chats

2–6 Bots; "Write normally to let the participating Bots decide who should respond", @mention to direct; "A Bot can send an asynchronous message to another Bot"; no switch to stop bot-to-bot messages `[docs chat-and-collaboration]`. All participants are Grok Bots.

### 3.6 Platform-native agent features and shared workspaces

- Slack: see §5.2. Third-party agents are admitted as apps.
- Mattermost Agents, Rocket.Chat AI: bring-your-own-LLM endpoint; see §5.5, §5.6.
- Dust: "Mentioning an agent triggers the agent to respond in the conversation." — workspace-built agents.
- Taskade: EVE "Orchestrates the chat and can call other agents."; agents built by workspace members.
- Shogo: "Shogo is an open-source platform for building AI agents that do real work."; SDK/clients MIT, server AGPL-3.0; channels "for people and AI agents together"; bring-your-own external agent not addressed in the README.
- Tokenrip, Nimbalyst, Notion, monday.com: shared files/tables/tasks via MCP; not conversation. Tokenrip's own comparison (2026-10) says Claude, ChatGPT, Codex, Grok, Cursor connect "each staying in its own app and connecting as itself"; the same article says none of Muse, Duet, dots and Grok Bot shares memory, files or tasks with the others, and that Muse documents no way for two people's Muses to work together (vendor article; a lead, not evidence).
- AgentProvision: title only; search snippets describe shared rooms where agents "run on the AI engine you choose" (Claude, Codex, Gemini, Copilot, Qwen, local), memory across rooms, 190+ tools, "workflows for repeat work". robots.txt disallows `/chat/`, `/agents/`, `/memory/`, `/workflows/`. One GitHub repo `ecolonco/agentprovisionmcpserver` (0 stars, pushed 2025-10-26), link to the product unverified.

---

## 4. Excluded projects (one line each)

- Glean: "Auto mode, Workflow mode, templates, the headless builder" with "triggers, steps, tools, flow, memory, and subagents" — fixed flow.
- CrewAI: "A crew in crewAI represents a collaborative group of agents working together to achieve a set of tasks." "Tasks are executed one after another".
- AutoGen: in-process group chat; `SelectorGroupChat` is one model call per hop over the whole transcript; every participant buffers every broadcast until selected (Appendix D).
- Google ADK, MetaGPT: task pipelines; parallel branches see only the fan-out point (Appendix D).
- OpenBot: local-first desktop app running Codex/Claude/Grok CLIs as teammates, all owned by the one person running it; in-app channels are a task tree (`channel_assign`, `channel_transfer`, `channel_reply`, `channel_result`); messaging connections present one bot identity per platform and a human cannot address a particular agent (Appendix D).
- munder-difflin: a "god" agent decomposes work into four-part contracts and is the sole writer of the shared board; `inform`/`done` terminal by prompt; `agree`/`refuse` in the schema, unused (Appendix D).
- jinn: org chart of "AI employees", single owner; lateral-send guard (10 per sender per 10 min, 12 relay hops), human message resets the hop chain (Appendix D).
- QM: one company bot shared by many humans; small-model YES/NO/REACT pre-check before an unprompted turn; Postgres turn queue with idempotency keys (Appendix D).

---

## 5. Baseline: the chat platforms' own bot models

Question: how far does a bare platform serve the yardstick with no integration layer. All from the platforms' own developer/admin docs; quotes capped at ~125 characters by the fetch tool.

### 5.1 Discord

- Someone else's agent as its own member: yes. "bot users are automated users that are 'owned' by another user". "Bots are added to guilds through the OAuth2 API, and cannot accept normal invites." "If marked as public, anyone with your bot's URL can add it to guilds in which they have proper permissions." Approver: a member with Manage Server `[snippet]`. "bot users do not have a limitation on the number of Guilds they can be a part of".
- Persistent process: Gateway WebSocket is the only path on which MESSAGE_CREATE is documented ("Gateway connections are persistent WebSockets"; heartbeat required). HTTP Interactions endpoint "does not require a connected client" but serves slash commands/components, "must send an initial response within 3 seconds"; the separate Webhook Events URL carries no guild channel message event. User flag BOT_HTTP_INTERACTIONS: "Bot uses only HTTP interactions and is shown in the online member list". So: serverless works for commands; reading free conversation needs a Gateway daemon.
- Mention-only: MESSAGE_CONTENT "is a unique privileged intent"; without it, content is blank except "Content in DMs with the app" and "Content in which the app is mentioned" — events still arrive, only content is blanked.
- Wake: push over WebSocket; HTTP push only for interactions.
- Anti-loop: not stated; bot-ignores-bot is a community recommendation. Rate: 50 requests/s; 120 gateway events per connection per 60 s. Tool permission outside chat: not stated.
- Native AI-agent concept: not stated.

### 5.2 Slack

- Third-party agent as its own member: yes, via app installation. "By default, a newly built Slack app can only be installed in its associated workspace"; "Activate Public Distribution" gives a shareable install URL; Marketplace is optional. "by default, members can install them without approval from a Workspace Owner"; owners can require approval. For agents: "By default, members can install apps with agents"; "guests can't use AI apps or agents". Multiple vendors' agents in one channel: "Click Add next to the apps and agents you want to add".
- Join prerequisites: Events API — "we'll send an HTTP POST request to your request URL", respond 2xx within three seconds, three retries; Socket Mode — WebSocket "without exposing a public HTTP Request URL" but "Apps using Socket Mode are not currently allowed in the public Slack Marketplace." Mention-only: `app_mention` "allows your app to subscribe to message events that directly mention your bot users"; "If your app is mentioned but not part of a conversation (and not invited to join), you won't receive an event." For agents: "@-mentions in channels can happen like they do today; whether you support this or not is up to you."
- Wake: platform pushes (HTTP POST or Socket Mode). Cap "30,000 per workspace/team per app per 60 minutes".
- Anti-loop: none at the platform. Bot messages are tagged ("bot_id and bot_profile fields"), not suppressed. The workflow-trigger page warns "it'll keep posting messages forever!"; Bolt's IgnoringSelfEvents skips the app's own messages only. Rate: "no more than one message per second per channel". Tool permission outside chat: not stated.
- Native agent concept: yes. "Agents are autonomous, goal-oriented AI apps that can reason, use tools, and maintain context across conversations"; dedicated surfaces (split view, top navigation); `assistant:write`; "Apps with agents can be built by third-party developers or by developers in your organization."; "Custom Agentforce agents can also be created in Salesforce and added to Slack."; "Once added, you can mention the agent in any message in that channel to start chatting." "Developing and using some AI features require a paid plan".

### 5.3 Matrix

- Owner on another homeserver: yes by architecture. "Room data is replicated across all of the homeservers whose users are participating in a given room."; federation `make_join` / `send_join`; cross-server invites signed by the invitee's homeserver. The spec pages read never mention bots; the only acknowledgement is "automated applications that cannot use a web browser" in client authentication.
- Join prerequisites: an ordinary user long-polls `/sync` with `next_batch` and `timeout`. HTTP push alternatives: Application Service ("This API is called by the homeserver when it wants to push an event … to the application service"), but "manually configuring the homeserver with information about the application service" — admin-provisioned; push gateway `/notify` is a notification channel, not a client channel. Mention-only delivery for `/sync`: not stated.
- Wake: pull (long-poll) for an ordinary user; push only for application services or via a push gateway.
- Anti-loop: not stated. Rate: "Homeservers SHOULD implement rate limiting"; M_LIMIT_EXCEEDED. Tool permission: not stated. Native AI-agent concept: not stated.

### 5.4 Telegram

- Someone else's bot in a group: yes. "Telegram bots are special accounts that do not need a phone number to set up."; "A user must either add them to a group or send them a message first."; owner-side gate "/setjoingroups".
- Join prerequisites: "two mutually exclusive ways of receiving updates" — getUpdates (long poll) or setWebhook ("we will send an HTTPS POST request to the specified URL"; ports 443/80/88/8443); "Incoming updates are stored on the server until the bot receives them either way" … "not … longer than 24 hours." Mention-only: privacy mode is the default; a bot receives "Commands explicitly meant for them", general commands if it was the last bot to post, inline messages, "Replies to any messages implicitly or explicitly meant for this bot", all service messages, all private chats, all channel posts where it is a member. A bare @mention is not in that list.
- Wake: webhook push or getUpdates pull; updates buffer 24 h.
- Anti-loop: the one platform-level rule found, with two docs pages in conflict. FAQ: "bots will not be able to see messages from other bots regardless of mode." Features page: opt-in "Bot-to-Bot Communication Mode" in @BotFather; "If at least one bot has Bot-to-Bot Communication Mode enabled, the receiving bot can receive and respond to the message."; "Bot-to-bot communication can easily result in infinite interaction loops. When enabling this feature, you must implement safeguards to ensure interactions terminate."; "Your bot must remain stable even if another bot intentionally responds instantly and continuously." Rate: "bots are not be able to send more than 20 messages per minute" in a group; ~30 messages/s broadcast.
- Native agent concept: marketing only ("AI bots can natively meet, chat and compete").

### 5.5 Mattermost

- Third-party agent as its own member: as a bot account, admin-gated. "Bot accounts are just like user accounts, except they:" "Can't be logged into."; "Only System Admins or plugins can create or manage bot accounts." The config page says users with Enable Bot Account Creation on can create bots in Integrations > Bot Accounts; the API says "Must have create_bot permission." Self-hosted default off / Cloud default on `[snippet]`. Bots authenticate with personal access tokens. An outside party cannot bring an account from elsewhere.
- Join prerequisites: persistent WebSocket `/api/v4/websocket` with `authentication_challenge`; events include `posted`; whether bots receive other bots' posts: not stated. Outgoing webhooks: POST on "one or more trigger words", "Select the public channel", first-word match, and "outgoing webhooks and slash commands… require a user account at this time" (not a bot). Mention-only delivery for bots: not stated.
- Wake: pull-style persistent socket; push only via trigger-word outgoing webhook posting as a user.
- Anti-loop for bots: not stated. Tool permission: only inside the Agents plugin — "Agents use built-in Mattermost tools, remote MCP servers with per-user OAuth"; "Tool results in channels can be shared or kept private by the person who asked."; "Agent-level MCP access is configured separately on each agent's MCPs tab."; "@mentioning an agent in a public channel can also allow tool calling there."
- Native "Agents": bring-your-own-LLM, not bring-your-own-agent. An agent = plugin-managed bot + configured LLM "service" ("LLM provider (OpenAI, Anthropic, AWS Bedrock, Cohere, Cohere North, Mistral, Scale AI, Azure OpenAI, OpenAI-compatible)"); "multiple agents can reuse the same service configuration."; "Deleting an agent deactivates the linked Mattermost bot account."; "Use local models through OpenAI-compatible APIs (Ollama, vLLM)". Who creates: "By default, regular users do not have manage_own_agent."; "Channel access restricts the channels in which the agent can be @mentioned." The MCP material goes the other direction — external clients into Mattermost: "Connect Claude Code, Claude Desktop, Cursor, VS Code, or any MCP client that supports streamable HTTP to Mattermost"; "The Mattermost MCP Server enables AI agents and external applications to interact with your Mattermost instance" — tool access, not membership.

### 5.6 Rocket.Chat

- Third-party agent as its own member: as a bot user, admin only. "Bots in Rocket.Chat requires user accounts with the bot role."; "Admins (and only admins) can create the bot account and configure role permissions."; separate "app" role for Marketplace apps. "Bots integration has been deprecated. We recommend building with Apps-Engine instead."
- Join prerequisites: Realtime API over WebSockets, `stream-room-messages`; bot stream updates "every time a message is sent either directly to them or any room they are joined in." But "We are no longer actively testing or maintaining these methods."; "we strongly recommend using the REST APIs instead." (pull only). Outgoing webhooks: "Message Sent" "Triggered when a message starts with one of the configured Trigger Words."; posts as a user that "must already exist in the workspace." Apps-Engine hooks (IPreMessageSentPrevent, IPreMessageSentModify, IPostMessageSent) run on the platform; "Starting with Rocket.Chat version 7.0, only workspaces on premium plans can install and use private apps."; cloud "only on a Dedicated private cloud workspace." Mention-only delivery: not stated.
- Wake: pull (REST) or deprecated socket; push via trigger-word webhook; in-process hooks for an installed app.
- Anti-loop, rate, tool permission: not stated.
- Native "Rocket.Chat AI": bring-your-own-LLM, single assistant: "currently in beta"; "you need a self-hosted LLM of your choice"; "users must bring their own LLM deployment"; "The bot responds in the thread."; "agent mode" calls "the configured endpoint" ("The agent pipeline handles internal orchestration"). Third-party agents as members: not stated.

### 5.7 Baseline conclusion

1. Every platform gives an agent an identity that is a member of the room, and someone other than the platform can own it; the approver is a server-side gate (Discord Manage Server; Slack any member unless app approval is on; Matrix room invite; Telegram any adder; Mattermost sysadmin or `create_bot`; Rocket.Chat admin only).
2. For open-ended conversation the agent must hold a persistent connection or poll on all six; HTTP push covers only a subset (Discord: interactions and a non-message webhook list; Slack: Events API, full; Telegram: webhook, full, buffered 24 h; Matrix: application service, admin-provisioned; Mattermost/Rocket.Chat: trigger-word outgoing webhooks posting as a user). Slack and Telegram are the two where a serverless agent can see ordinary room talk.
3. Mention-only delivery is a real mode only on Slack (`app_mention`) and Telegram (privacy mode: commands and replies, not bare mentions); Discord blanks content rather than withholding events; Matrix, Mattermost, Rocket.Chat: not stated.
4. The only platform-native anti-loop rule is Telegram's (bots do not see other bots unless Bot-to-Bot mode is opted into, and the developer must build termination safeguards); the other five have none — bot-ignores-bot is a convention or an SDK default. No platform has a who-speaks or turn-taking concept; what exists is per-channel send rate (Slack 1/s/channel, Telegram 20/min/group, Discord 50 req/s global).
5. Native "AI agent" concepts exist on Slack (third-party agents admitted, Marketplace-distributed, paid plan), Mattermost (platform-run harness, bring-your-own LLM endpoint; MCP connects outside agents to Mattermost as tools, not members) and Rocket.Chat (single bring-your-own-LLM assistant). None of the six admits an externally run harness with its own memory and skills as anything more than an ordinary bot account, and none defines a per-bot tool-permission concept for actions outside chat (Mattermost Agents has one, only for agents it runs itself).

---

## 6. Personal agents: the minimum surface for taking part in a room they do not own

For each agent: what a third party could use to get it into a shared room. "Persistent process" is split into (a) the vendor keeps a VM alive for the agent, and (b) the owner can run a long-lived listener on it; (b) is not stated for any hosted agent.

### 6.1 Meta Muse (muse.ai)

| surface | exists? | what a third party can do with it | evidence |
|---|---|---|---|
| inbound trigger | not stated | No Muse-facing API, webhook or event ingress on official pages. Connector Platform docs describe only Muse calling the connector. | platform.muse.ai/docs [docs]; muse.ai [marketing] |
| remote MCP connector | custom connector: yes; user-added remote MCP URL: not stated | Owner-side: "If you want to connect to a service not yet available in the Connector list, you can ask Muse to create a Custom Connector. Muse will guide you through the process, which can involve retrieving API information from the service. Muse stores these in its Secure Credentials Store." Meta does not review custom connectors. Developer-side: submit to the Connector Platform with "the endpoint, authentication setup, requested scopes, and credentials Muse needs", "API or MCP documentation", OAuth client credentials or API key; three-stage review. Whether a user can paste a remote MCP URL in Settings: not stated; third-party guides conflict. | help 1687253048996149 [docs, via snippet]; platform.muse.ai/docs [docs]; safety blog: "Muse can also write its own custom connectors for other services you care about if they have their own APIs or CLIs" [marketing] |
| scheduled / heartbeat | yes | "Create recurring tasks: Schedule something to repeat daily, weekly, or on a custom interval." VM built for "concurrent sub-agents and crons". Minimum interval and whether a scheduled task may invoke a connector: not stated. | help 1484325780075655 [docs, via snippet]; safety blog [marketing] |
| outbound posting to chat platforms | only via owner-built connector | Reach surfaces: the Muse app and WhatsApp ("Chat with your agent using the Muse app on mobile, desktop or directly in WhatsApp"). Built-in connectors named: email, calendar, Instagram; press adds Gmail/Google Calendar/Ticketmaster. Slack/Discord/Mattermost/Rocket.Chat/Matrix: not stated. By default Muse asks before significant actions such as sending email. | muse.ai [marketing]; help 1385290430137537 [docs, via snippet] |
| persistent process | (a) yes; (b) not stated | "Muse Secure VM is a persistent, isolated Linux computer with a full browser"; VM "runs the core Hatch daemon and systemd-managed services", "backed up continuously". | muse.ai, safety blog [marketing] |
| skill / instruction file | partial | Built-in connectors "are guided by SKILLs, which are detailed instructions for Muse". A user-editable standing-instructions file: not stated. | safety blog [marketing] |

Poppy / Personal Agent Protocol: agent→business only. "covers how a personal agent identifies itself, gets the customer's permission, and works with a company across any kind of task"; discovery via "/.well-known/poppy.json"; interfaces "OpenAPI and MCP" plus an optional company agent. No personal-agent↔personal-agent, group or room part. Conflicts: Sierra blog says "companies can hold a request open until there's news to share" `[marketing]`; personalagentprotocol.org lists "Push notifications" as "Not started in the spec" and says "Today a Personal Agent only learns about a change by checking again" `[docs]`; press calls it Draft 0.1 while the protocol site states Version 1.0 is published.

A Muse agent is known to have joined a Mattermost room through a connector its owner built. The documented surfaces that compose into that: Custom Connector against the Mattermost REST API (credentials in the Secure Credentials Store) + a recurring scheduled task on the persistent VM. The joint itself (a scheduled task invoking a custom connector) is not documented, and no inbound half exists, so the documented pattern is poll-and-reply.

Lightest viable way today: the owner asks Muse to build a Custom Connector against the room's REST API and sets a recurring task to check the room and reply.

### 6.2 Anthropic Claude Duet

| surface | exists? | what a third party can do with it | evidence |
|---|---|---|---|
| inbound trigger | not stated | No API, webhook or external trigger on the announcement or release notes. | claude.com Duet article [marketing]; release notes 2026-10-21 [docs] |
| remote MCP connector | not stated for Duet | Duet "can use the over 6,000 connectors and plugins in the Claude Marketplace" and carries over "your memory, skills, and connected apps from your Claude account". Whether a custom remote MCP connector is usable inside Duet: not stated. | [marketing]; custom-connector help article (no Duet mention) [docs, via snippet] |
| scheduled / heartbeat | yes, vendor cadence | "Each morning, it sends you a short brief"; "Claude Duet checks your connected accounts every few hours"; "You can also tell it what you want to hear about and how often." User-defined cron or minimum interval: not stated. | [marketing] |
| outbound posting | not stated as posting | Slack appears as something it reads and drafts for: "Find the Gmail and Slack messages you've missed and help you reply." Before sending, paying, signing or deleting "it shows you exactly what it's about to do and waits for your approval." Discord/Mattermost/Rocket.Chat/Matrix: not stated. | [marketing] |
| persistent process | (a) yes; (b) not stated | "Claude Duet has a virtual machine with its own browser, so it doesn't require your laptop or phone to be on." | [marketing] |
| skill / instruction file | partial | Carries over "skills" from the Claude account; a Duet-specific instruction file: not stated. | [marketing] |

Lightest viable way today: none found as a room participant. Nearest pattern: Duet reads Slack on its every-few-hours check and drafts replies the owner approves one by one.

### 6.3 OpenAI dots

| surface | exists? | what a third party can do with it | evidence |
|---|---|---|---|
| inbound trigger | partial (connector-side events) | No webhook/API. "you can also specify a time or a supported event that should trigger work"; "When a connected service supports event monitoring, you can instead ask your dot to respond to a specific event" — Slack example: "investigate new bug reports in a connected Slack channel". "Connecting Slack or another source alone doesn't create a monitoring task." | learn.chatgpt.com/docs/dots/tasks-and-memory [docs] |
| remote MCP connector | not stated | "Your dot can use supported plugins installed and enabled for your account." Custom/remote MCP for dots: not stated. | /docs/dots, /computers-and-apps [docs] |
| scheduled / heartbeat | yes | "Work that repeats at fixed times needs a saved schedule"; what to check, when (time zone, end date), what deserves a notification, where to deliver; "Your dot can decide when to pause and wake up to continue work". Minimum interval: not stated. | [docs] |
| outbound posting | Slack, Teams | "In Slack, you can send a direct message or add your dot to a channel and mention it in a thread." "By default, your dot responds to you in Slack. You can instruct it to engage with others." "Your dot can message you privately to ask what you're comfortable sharing before it replies in a channel." Teams: "You can message your dot in Slack and Microsoft Teams" (no detail). Texting: "Your dot can only text you, and you're the only one who can text your dot"; "Group chats ... aren't currently supported through texting." Discord/Mattermost/Rocket.Chat/Matrix: not stated. | /docs/dots/channels [docs] |
| persistent process | (a) yes; (b) not stated | "Your dot lives in the cloud and has its own computer and browser." A connected owner computer must stay online with the ChatGPT app open "while your dot uses it". | [docs] |
| skill / instruction file | partial | Instructions are conversational; the dot keeps its own notes; "Local skills require a connected computer". Named instruction file: not stated. | [docs] |

Lightest viable way today: the owner adds the dot to a Slack channel, tells it to "engage with others", and sets a monitoring task naming the channel and what to watch for. Slack (possibly Teams) only.

### 6.4 Instinct

All cells not stated on official pages (see §3.4). Lightest viable way today: none found. The group-chats feature is Instinct's own group agent inside its texting thread.

### 6.5 xAI Grok Bot

| surface | exists? | what a third party can do with it | evidence |
|---|---|---|---|
| inbound trigger | yes (routine triggers) | Verified: webhook — "Send an HTTP POST to that URL with the header and, optionally, a JSON body."; a `200` means "Grok Bot accepted the call and started a run, not that the Bot has finished."; the key goes in a Bearer header. Email to the Bot's mail.grokbot.com address ("Anyone can send mail to the address"; "a routine can watch for it"). Slack: "A routine can start when the Bot is mentioned, when a word or phrase is mentioned." Limits: 50 routines per Bot; "Routine schedules must be at least five minutes apart."; 300 runs/hour. A chat platform's stock outgoing webhook would need to send the Bearer header — a relay may be needed, not verified. | docs.x.ai/grok-bot/skills-routines-and-automations [docs] verified |
| remote MCP connector | yes | "Add a custom connector" in Connect apps with Type Remote HTTPS (Server URL) or Command; a Bot can add a server "by URL or by a command". Private-network/private-CA servers ask for approval; team MCP policy can block. | /computer-and-apps [docs] |
| scheduled / heartbeat | yes | Routines in plain words ("every weekday at 8:00 AM"); verified: "Routines run in the cloud while your laptop is closed."; min 5-minute spacing. | [docs] verified |
| outbound posting | Slack (Team Bots), Teams (setup), X Chat; in-app group chats | Slack: after a Bot is shared as a Team Bot and connected, "teammates can message the Bot directly or mention it in channels"; "the Team Bot acts for the teammate who mentioned it"; replies post into the same thread and it "follows that thread and answers later replies without an @mention". Org-shared Bots: "any full member of your Slack workspace can mention it". Gates: personal Bots only chat with their owner; Slack guests, Slack Connect channels and other workspaces are never included; "Bots can prepare Slack messages as drafts. Desktop users choose Send message". Teams: admin approval, no behaviour detail. Discord: in development per TestingCatalog `[third-party, snippet]`. Mattermost/Rocket.Chat/Matrix: not stated. | /team-bots, /chat-and-collaboration [docs]; grok-bot-101 [marketing] |
| persistent process | (a) yes; (b) not stated | "Work continues while your laptop is closed, and files, sign-ins, and browser sessions persist between tasks"; "The computer is assigned to your user account, not an individual Bot." | [docs] |
| skill / instruction file | yes | "A skill is a reusable set of instructions for how to do a task"; private skills are "one library shared by all your Bots"; "Teach a task" records a browser workflow. "A bot is a set of instructions"; permission rules "in natural language in Settings > General > Agent". File format: not stated. | [docs], [marketing] |

Lightest viable way today: on Slack, share the Bot as a Team Bot; anyone in the channel mentions it and it replies in-thread. On any other platform: a webhook-triggered routine (POST + Bearer) plus a custom remote MCP connector to post back — a composition, relay not verified.

### 6.6 OpenClaw

| surface | exists? | what a third party can do with it | evidence |
|---|---|---|---|
| inbound trigger | yes | Gateway hooks (disabled by default): "POST /hooks/wake" ("a trusted notification for the selected agent's main session"), "POST /hooks/agent" (agent turn, sessionMode default isolated), "POST /hooks/<name>" via hooks.mappings. Auth: "Authorization: Bearer <token>" or "x-openclaw-token"; query-string token rejected. Delivery straight to a channel with `channel` + `to`. Also an OpenAI-compatible `/v1/chat/completions` on the gateway and an A2A (JSON-RPC) channel. External event content is to be treated as data. | docs.openclaw.ai/automation/cron-jobs/webhooks, /gateway, /channels [docs] |
| remote MCP connector | yes | `mcp.servers` entry with `url` + transport "streamable-http" or "sse"; `openclaw mcp add docs --url https://mcp.example.com/mcp --transport streamable-http`; auth "oauth" + `openclaw mcp login`. No server-pushed events. | /tools/mcp [docs] |
| scheduled / heartbeat | yes | Automations: "one-shot reminders, recurring intervals and cron expressions, and inbound webhook triggers"; first-setup proactive check "runs every 30 minutes unless the provider supplies a different initial cadence"; "Return NO_REPLY when a check has no visible update"; delivery.target "owner" "does not follow a group conversation as channel: 'last' can". HEARTBEAT.md is legacy: "runtime does not parse them as schedules or read HEARTBEAT.md". | /automation, /heartbeat [docs] |
| outbound posting | Mattermost, Slack, Discord, Matrix (official plugins); Rocket.Chat community only | Mattermost: "@openclaw/mattermost", "bot token + WebSocket events", `channels.mattermost {botToken, baseUrl}`. Matrix: "@openclaw/matrix" on matrix-js-sdk. Slack: Socket Mode / HTTP Request URLs / relay. Discord: bot token + Message Content intent. Group reply rule: "Default is groupPolicy: 'allowlist'; if your group allowlist is empty, group messages are blocked"; replies "require a mention unless you disable mention gating"; Mattermost chatmode oncall (default, "only when @mentioned") / onmessage / onchar; thread follow-ups without mention for 7 days; Matrix `groups.<room>.requireMention` / `autoReply`. | /channels, /channels/mattermost, /channels/matrix, /channels/groups [docs]; Rocket.Chat plugins [third-party] |
| persistent process | yes (owner-run daemon) | "One always-on process for routing, control plane, and channel connections"; `openclaw gateway install` → launchd / systemd user unit / Windows Scheduled Task. | /gateway [docs] |
| skill / instruction file | yes | Per-agent workspace with "AGENTS.md Operating instructions + 'memory'", SOUL.md, IDENTITY.md, USER.md; skills and plugins. | /concepts/multi-agent [docs] |

Multi-agent-in-one-room rules `[docs]`: `allowBots` — "Discord, Mattermost, and Slack default to accepting them under the normal mention and access rules"; "mentions" requires an explicit mention and "Thread participation does not count, so two bots cannot keep waking each other in a thread"; `false` ignores bot posts. `botLoopProtection` defaults enabled, maxEventsPerWindow 20, windowSeconds 60, cooldownSeconds 60 (sliding window; per-room overrides). The Mattermost page suggests `allowBots: "mentions"` with a longer window "for two agents sharing a channel". Routing: "For shared groups, bind the group to one agent or use Broadcast groups"; `agents.entries.*.groupChat.mentionPatterns` "useful when multiple agents share a group". Internal agent-group rounds capped at 4 rounds / 32 turns.

Lightest viable way today: install the official channel plugin (e.g. @openclaw/mattermost) with a bot token, run the gateway daemon, have the room owner invite the bot; it answers when @mentioned.

### 6.7 Hermes Agent (Nous Research)

| surface | exists? | what a third party can do with it | evidence |
|---|---|---|---|
| inbound trigger | yes | Webhook adapter: POST "http://your-server:8644/webhooks/<route-name>"; "Every route must have a secret"; signature schemes github / gitlab / linear / generic (HMAC, "X-Webhook-Signature-V2" + timestamp); bad signature → 404. Modes: agent run (default), deliver_only, cron_job ("202 Accepted"), coalesce. Deliver targets include telegram, discord, slack, matrix, mattermost, email. A stock chat-platform outgoing webhook would have to produce that signature — relay may be needed, not verified. | /docs/user-guide/messaging/webhooks [docs] |
| remote MCP connector | yes | `mcp_servers: remote_api: url: "https://mcp.example.com/mcp"` with headers or `auth: oauth`; the gateway "watches config.yaml". | /features/mcp [docs] |
| scheduled / heartbeat | yes | "Cron execution is handled by the gateway daemon", which "ticks the scheduler every 60 seconds, running any due jobs in isolated agent sessions"; `/cron add "every 2h" "…"`; deliver to origin / platform home channel / `telegram:<chat_id>` (Slack, Matrix, Mattermost supported); `continuity=true` carries last output. | /features/cron [docs] |
| outbound posting | Mattermost, Slack, Discord, Matrix built in; Rocket.Chat community plugin | Mattermost: REST v4 + WebSocket via MATTERMOST_URL / MATTERMOST_TOKEN (bot or personal access token); "Without a mention, Hermes ignores the message" (MATTERMOST_REQUIRE_MENTION default true; MATTERMOST_FREE_RESPONSE_CHANNELS). Discord: require_mention default true, free_response_channels, thread_require_mention default false. Slack: Socket Mode, "only responds when @mentioned", thread follow-ups without mention. Matrix: mautrix, require_mention default true, auto-accepts invites. "By default, the gateway denies all users who are not in an allowlist or paired via DM." Rocket.Chat: community plugin "rocketchat-team-gateway" (Ulx Kft., "changes nothing in Hermes core"), long-lived WebSocket + REST, "A bot answers only in rooms it is a member of". | /messaging/{index,mattermost,discord,slack,matrix} [docs]; /plugins/rocketchat-team-gateway [docs listing] |
| persistent process | yes (owner-run daemon) | "The gateway is a single background process that connects to all your configured platforms, handles sessions, runs cron jobs"; systemd / launchd / Task Scheduler install. | /messaging/ [docs] |
| skill / instruction file | yes | "SOUL.md is the primary identity" at ~/.hermes/SOUL.md; AGENTS.md for project guidance; per-channel system_prompt overrides; skills via /<skill-name>. | /features/personality, /messaging/ [docs] |

Multi-agent-in-one-room rules `[docs]`: `allow_bots` none (default on Discord and Slack) / "mentions" ("safest bot-to-bot collaboration mode") / all; DISCORD_BOTS_REQUIRE_INLINE_MENTION default true ("Reply metadata alone does not start one"); Mattermost recognises `from_bot` posts and MATTERMOST_BOT_USERS for token-based peer bots, MATTERMOST_ALLOW_BOTS=all|mentions "only when bot-to-bot messages are intentional". Loop guard figures conflict across pages: overview `gateway.bot_loop_guard` max_hops default 4; Discord page: 20 bot messages in one channel within 5 minutes → bot messages dropped for 10 minutes. Bot Mode (desktop, on by default): rooms of "2–6 Bots", "Your message triggers up to three serial rounds of member turns", "@-mentioned Bots respond (everyone responds when nobody is mentioned)", Room setting Everyone / lead / automatic, hard caps "10 messages per send, 3 rounds"; on external platforms "bots message each other fine over the messaging platform, but the agent-side message_agent tool never appears". Silent replies via "[SILENT]" / "NO_REPLY".

Lightest viable way today: set the built-in adapter's env (MATTERMOST_URL + MATTERMOST_TOKEN, DISCORD_BOT_TOKEN, Slack xoxb+xapp, MATRIX_ACCESS_TOKEN), allowlist users/channels, run `hermes gateway`; the room owner invites the bot; it answers when @mentioned.

### 6.8 Group-chat behaviour add-ons seen

- Humalike x Hermes (Product Hunt) `[marketing/third-party]`: a Hermes plugin for Slack/Telegram/WhatsApp groups that "Decides, when to jump in and when to stay quiet" — a lightweight should-I-reply check per message, full pipeline only when it has something to say; paces replies with typing delays; "1-3 short messages instead of a wall"; learns group tone. No multi-agent rule.
- Linq "Building AI agents that work in group chats" `[third-party]`: "separate the decision to respond from the generation of the response" — a cheap classifier (Claude Haiku in the example) labels each message respond / react / ignore; treat name, misspellings, "AI/bot/assistant" and follow-ups to the agent's last message as addressing it; default to respond when unsure; break reaction loops by preferring text; prefix history with sender handles.

### 6.9 Summary across personal agents

1. Common to most: a vendor-kept persistent VM (Muse, Duet, dots, Grok Bot) or an owner-run daemon (OpenClaw, Hermes), and scheduled polling — user-defined for Muse, dots, Grok Bot, OpenClaw, Hermes; vendor cadence "every few hours" for Duet; not stated for Instinct.
2. Remote-MCP pulls are documented for Grok Bot, OpenClaw and Hermes; Muse only as "ask Muse to build a Custom Connector" or developer submission; Duet and dots: not stated.
3. Inbound wake is rare: Grok Bot (webhook with Bearer, email, Slack triggers), OpenClaw (`/hooks`) and Hermes (HMAC webhooks); dots offers only connector-side event monitoring (Slack); Muse, Duet, Instinct: none stated.
4. Native presence in a shared room: among the hosted agents only Slack is documented as a room the agent can be added to and reply in (dots, Grok Team Bots), with gates (dots "responds to you" by default; Grok personal Bots only chat with their owner; Duet and Muse ask before sending). None of the five hosted agents documents Mattermost, Rocket.Chat, Discord or Matrix. OpenClaw and Hermes cover all four; Rocket.Chat is community-plugin-only for both.
5. Rules for several bots in one room exist only in the open-source agents (allowBots / allow_bots, loop guards, bind-a-group-to-one-agent) and in Grok Bot's in-app group chats; the hosted agents' single-agent reply gates differ, and none states a multi-bot rule.

---

## 7. Cross-cutting findings

### 7.1 Four join doors, converging across products

| door | direction | what the agent must be able to do | who offers it |
|---|---|---|---|
| remote MCP connector | pull; the agent must be running | add an MCP server URL (+ bearer) | Agent Room, Aurmada seats, Ando, Conduyt (MCP endpoint), Grok Bot, OpenClaw, Hermes |
| skill.md + REST heartbeat | pull on a schedule | plain HTTPS with a bearer key; a scheduler | Moltbook, Aurmada agents, Agent Room network door (long-poll variant), AgentChat |
| listener / webhook / WebSocket | push; the owner runs a process or exposes an endpoint | a daemon, or an HTTPS endpoint that accepts a signed POST | Conduyt `agent listen`, Ando (HTTPS receiver, WebSocket), OpenClaw `/hooks`, Hermes webhooks, Grok Bot routine webhook, Slack Events API, Telegram webhook |
| MCP Events `agent.wake` | push into an MCP-capable chat app | an MCP client that supports `events/subscribe` | Ando, Aurmada (ChatGPT Work chats) |

### 7.2 Who-speaks mechanisms seen

| mechanism | where |
|---|---|
| mention-only, answer once per turn | Conduyt, Buzz (relay gate), Hermes/OpenClaw defaults, Slack `app_mention`, Telegram privacy mode |
| consecutive-agent-post cap with a "needs a human" pause | Conduyt (6), Aurmada (20 turns without a human; `429 ai_turn_cap`) |
| sliding-window burst guard | OpenClaw (20 / 60 s), Hermes (20 / 5 min on Discord), Aurmada (3 per AI / 10 s, 6 per room / 10 s) |
| wait for the conversation to pause | Agent Room (5 s settle, 30 s cap) |
| per-agent notify ceiling | Ando (`mentions_and_direct_messages` default) |
| a model deciding relevance | Ando participation judge (its own agents only); Humalike; Linq's respond/react/ignore classifier; QM's YES/NO/REACT pre-check |
| prompt text only | Buzz ("silence is usually correct") |
| platform-native bot-sees-bot rule | Telegram only |

### 7.3 Ownership models

- Owner runs the harness, platform guarantees identity and attribution: Ando external agents, Conduyt, Agent Room, Aurmada, Buzz.
- Platform runs the harness, owner picks the LLM: Ando-run agents, Mattermost Agents, Rocket.Chat AI, Shogo, Bloome's own agents, AgentProvision (per snippets).
- Vendor runs the agent, owner has no process to run and no inbound trigger: Muse, Duet, dots, Instinct (Grok Bot is the exception with a webhook).
- A per-agent tool-permission concept for actions outside chat exists only where the platform runs the harness (Ando-run agents, Mattermost Agents) or scopes data access (Conduyt's CRM visibility).

### 7.4 Documentation reliability observed

- Ando: marketing lists Claude/Codex as external agents; docs say they never become members. Notify options differ between pages.
- Aurmada: heartbeat cadence 180 min (skill.json) vs 30–60 min (llms.txt).
- Conduyt: `turnToken`/`replyIdempotencyKey` (blog) vs claim token / `--idempotency-key` (CLI README).
- Bloome: four expansions of "ACP".
- Hermes: loop-guard figures differ between the overview and the Discord page.
- Telegram: FAQ vs features page on bot-sees-bot.
- Poppy: "hold a request open" (Sierra) vs "only learns about a change by checking again" (protocol site); Draft 0.1 (press) vs 1.0 (site).

---

## 8. Sources

Ando: https://www.ando.so/ai-agents-in-team-chat · https://www.ando.so/features/bring-your-own-agent · https://www.ando.so/features/ai-agents · https://www.ando.so/learn · https://www.ando.so/learn/ando-agent-vs-external-agent · https://www.ando.so/agents/mcp · https://www.ando.so/agents/claude-code · https://www.ando.so/agents/codex · https://www.ando.so/faq · https://www.ando.so/pricing · https://www.ando.so/blog/introducing-ando · https://docs.ando.so/ · https://docs.ando.so/llms.txt · https://docs.ando.so/docs/agents · https://docs.ando.so/docs/external-agents · https://docs.ando.so/docs/external-agents/another-agent.md · https://docs.ando.so/docs/external-agents/openai-dots.md · https://docs.ando.so/docs/external-agents/claude-chatgpt-and-codex.md · https://docs.ando.so/developers/running-agents.md · https://docs.ando.so/developers/realtime.md · https://docs.ando.so/docs/ando-mcp/tools.md · https://docs.ando.so/developers/agent-skills/ando-looped-channel.md · https://www.indexventures.com/perspectives/ando-building-the-interface-layer-for-human-and-agent-teams

Agent Room: https://agentroom.chat/ · https://agentroom.chat/llms.txt · https://agentroom.chat/agents.txt · https://agentroom.chat/guides/agents-working-together/ · https://agentroom.chat/guides/claude-code-codex-one-room/ · https://agentroom.chat/guides/chatgpt-with-coding-agents/ · https://github.com/rainyflash/agent-room · README, docs/self-hosting.md, docs/architecture.md, apps/agent-room-mcp/README.md · GitHub API repo and releases

Aurmada: https://rooms.aurmada.com/ · https://luma.com/z0nwx9ba · https://rooms.aurmada.com/skill.md · https://rooms.aurmada.com/skill.json · https://rooms.aurmada.com/llms.txt · https://rooms.aurmada.com/llms-full.txt · https://rooms.aurmada.com/heartbeat.md · https://rooms.aurmada.com/round.md · https://rooms.aurmada.com/welcome · https://rooms.aurmada.com/r/lobby · https://rooms.aurmada.com/agents · https://rooms.aurmada.com/changelog · https://rooms.aurmada.com/paid · https://rooms.aurmada.com/live

Conduyt: https://dev.to/conduyt/we-put-three-ai-agents-from-three-vendors-in-one-chat-thread-4pac · https://conduyt.com/features/team-chat/ · https://conduyt.com/resources/ai-agents-working-together/ · https://conduyt.com/resources/connect-your-ai-to-team-chat/ · https://conduyt.com/resources/how-to-use-claude-code-with-your-team/ · https://conduyt.com/resources/bring-your-own-ai-agent-crm/ · https://conduyt.app/docs · https://unpkg.com/conduyt/README.md · https://registry.npmjs.org/conduyt

Bloome: https://bloome.im/features/multi-agent · https://bloome.im/features/ai-agents-in-group-chat · https://bloome.im/features/claude-code · https://bloome.im/features/codex · https://bloome.im/features/opencode · https://bloome.im/features/ai-agent-platform · https://bloome.im/guides/multi-agent-orchestration · https://bloome.im/guides/ai-coding-agent · https://bloome.im/guides/agent-to-agent-communication · https://bloome.im/blog/agent-collaboration-protocol · https://bloome.im/sitemap.xml · https://www.testingcatalog.com/bloome-launches-chat-platform-for-ai-agent-teams/

Shogo lead list: https://shogo.ai/blog/ai-agent-platforms-team-chat/ · https://shogo.ai/ · https://github.com/shogo-labs/shogo-ai · https://docs.slack.dev/ai/ · https://docs.slack.dev/ai/developing-ai-apps/ · https://docs.slack.dev/quickstart/ · https://learn.microsoft.com/en-us/microsoftteams/platform/teams-sdk-overview · https://developers.google.com/workspace/chat/overview · https://docs.mattermost.com/end-user-guide/agents.html · https://docs.rocket.chat/docs/mcp-server · https://docs.dust.tt/docs/user-documentation/pods/conversations · https://docs.taskade.com/ai-features/ai-features/multi-agents · https://docs.glean.com/agents/ · https://docs.crewai.com/en/concepts/crews

Moltbook: https://en.wikipedia.org/wiki/Moltbook · https://www.moltbook.com/ · https://www.moltbook.com/skill.md

Shared workspaces: https://tokenrip.com/blog/best-ai-agent-workspace-tools · https://tokenrip.com/blog/personal-ai-agents-compared

Platforms: Discord https://docs.discord.com/developers/events/gateway · /events/gateway-events · /events/webhook-events · /interactions/overview · /interactions/receiving-and-responding · /topics/oauth2 · /topics/rate-limits · /resources/user · /topics/community-resources. Slack https://docs.slack.dev/apis/events-api/ · /apis/events-api/using-socket-mode/ · /ai/ · /ai/agents · /ai/developing-ai-apps · /reference/events/app_mention/ · /reference/events/message/ · /reference/events/message/bot_message/ · /apis/web-api/rate-limits/ · /app-management/distribution/ · /tools/bolt-python/reference/ · /tools/deno-slack-sdk/guides/creating-event-triggers · https://slack.com/help/articles/222386767 · https://slack.com/help/articles/33076000248851. Matrix https://spec.matrix.org/latest/ · /client-server-api/ · /application-service-api/ · /push-gateway-api/ · /server-server-api/. Telegram https://core.telegram.org/bots · /bots/api · /bots/faq · /bots/features. Mattermost https://docs.mattermost.com/developers/integrate/reference/bot-accounts · /developers/integrate/webhooks/outgoing · /api/reference/mattermost-api · /api/reference/create-bot · /administration-guide/configure/integrations-configuration-settings.html · /agents/docs/admin_guide · /agents/docs/features/managing_agents.html · https://github.com/mattermost/mattermost-plugin-agents. Rocket.Chat https://developer.rocket.chat/docs/bots-architecture · /docs/realtime-api · /apidocs/realtimeapi · /api/realtime-api/subscriptions/stream-room-messages · /docs/message-event-interfaces · /docs/rocketchat-apps-engine · https://docs.rocket.chat/docs/integrations · /docs/roles-in-rocketchat · /docs/rocketchat-private-apps · /docs/rocketchat-ai-app · /docs/rocket-chat-ai-faq · /docs/rocketchat-ai-app-setup-guide

Personal agents: Muse https://muse.ai/ · https://muse.ai/blog/security-and-safety-for-ai-agents-our-approach-with-muse · https://platform.muse.ai/ · https://platform.muse.ai/docs · https://sierra.ai/blog/poppy · https://personalagentprotocol.org/ · https://personalagentprotocol.org/docs/introduction · Meta Help Center 1687253048996149, 1484325780075655, 1385290430137537 (via snippets). Claude Duet https://www.claude.com/resources/articles/claude-duet · https://support.claude.com/en/articles/12138966-release-notes. dots https://learn.chatgpt.com/docs/dots · /docs/dots/channels · /docs/dots/tasks-and-memory · /docs/dots/computers-and-apps. Instinct https://www.instinct.com/ · https://instinct.com/privacy-policy · https://techcrunch.com/?p=3173877. Grok Bot https://x.ai/bot/guides/grok-bot-101 · https://docs.x.ai/grok-bot/skills-routines-and-automations · /grok-bot/team-bots · /grok-bot/chat-and-collaboration · /grok-bot/computer-and-apps. OpenClaw https://docs.openclaw.ai/channels · /concepts/features.md · /channels/mattermost · /channels/matrix · /channels/groups · /channels/bot-loop-protection · /gateway · /automation · /heartbeat · /automation/cron-jobs/webhooks · /concepts/multi-agent · /tools/mcp. Hermes https://hermes-agent.nousresearch.com/docs/user-guide/messaging/ · /messaging/webhooks · /messaging/mattermost · /messaging/discord · /messaging/slack · /messaging/matrix · /features/cron · /features/mcp · /features/personality · /bot-mode · /plugins/rocketchat-team-gateway · https://www.producthunt.com/p/humalike-2/humalike-x-hermes · https://implexa.ai/s/clawhub/agentchat-skill · https://linqapp.com/blog/building-ai-agents-that-work-in-group-chats

Not readable: npmjs.com/package/conduyt (403); docs.mattermost.com/configure/agents-admin-guide.html (empty); developers.glean.com/agents (404); docs.rocket.chat/docs/rocketchat-ai (404); help.taskade.com article (TOC only); docs.dust.tt/docs/conversations (404); rooms.aurmada.com/how (403); agentprovision.com/docs, /pricing, /blog, /llms.txt (title only); docs.ando.so/docs/mcp (404); help.openai.com/en/articles/20001530 (403); openai.com/index/introducing-dots/ (403); instinct.com/blog (500); grok.com/help/grok-bot/routines (landing only); Discord help "Using Apps on Discord" (403).

---

## 9. Out-of-band channel between a monitor and a remote agent (added 2026-11-17)

Question: a long-running monitor outside the agent needs (a) to wake the agent and (b) ideally to ask it something and get an answer back outside the chat platform (e.g. its profile). What exists?

### 9.1 Protocols

| Protocol | Wakes an agent that is not connected? | Ask-and-answer out of band? | Transport | Agent host must run | Adoption |
|---|---|---|---|---|---|
| MCP core rev 2026-07-28 | No. "servers do not initiate JSON-RPC requests and clients do not send JSON-RPC responses" [docs transports]. `subscriptions/listen` is a client-held stream; sessions, GET stream, `Last-Event-ID`, `ping` removed [docs streamable-http, changelog] | Only inside a client request: `sampling`/`elicitation` are `InputRequiredResult` round-trips the client retries [docs] | stdio; Streamable HTTP (POST per message, request-scoped SSE) | nothing listening; the agent is the client | Claude Code v2 speaks 2026-07-28 [docs]; Codex opt-in `mcp_2026_07_28` [PR #35724]; OpenCode default legacy ≤2025-11-25 [docs] |
| MCP legacy ≤2025-11-25 | No; server→client requests rode the client-opened GET stream, only while held [docs] | Yes while connected | stdio; Streamable HTTP + `Mcp-Session-Id` | nothing listening | every listed client |
| **MCP Events, SEP-3415 "Events Extension"** (not in the published spec) | **Yes in webhook mode**: `events/subscribe` registers a callback; subscription outlives the connection. "Three delivery modes, none mandatory:" poll, push, webhook; "Opaque cursors let a client resume after a disconnect."; webhooks "signed per Standard Webhooks" [docs PR #3415]. Status: "SEP accepted by core maintainers, but still requires final wording and reference implementation." | No; one-way | Streamable HTTP; webhook = HTTPS POST | **a public HTTPS receiver** (webhook mode is the only one shipped). ChatGPT: "Require HTTPS for callbacks."; "Block private, local, and other non-public addresses"; "Retry transient failures with exponential backoff and bounded attempts." [docs, verified] | **ChatGPT**: "ChatGPT supports webhook delivery and callback verification." / "Polling, streaming … are not supported" / "available in Work chats on ChatGPT web, in Work chats in the desktop app with Cloud selected, and with dots" [docs, verified]. **OpenClaw** `mcp-events` plugin (OpenAI webhook profile) [docs]. **OpenCode v2.3.0** "experimental support for MCP events, behind the `mcp_events` flag" [changelog]. Vercel AI SDK experimental. Claude Code, Codex, Hermes, Grok Bot, Muse: not found |
| ACP v1 / v2 draft | No. "The protocol currently defines one transport mechanism for agent-client communication: stdio"; "The client launches the agent as a subprocess." [docs]. Remote: RFD "Streamable HTTP & WebSocket Transport", Active, "In-flight messages are not replayed." | Yes from the spawning client (`session/prompt` returns text). `agentInfo` = `name`, `title`, `version` only [docs initialization] | stdio JSON-RPC; HTTP/WS proposed | for remote use: a bridge on the agent host (acpremote "Generic remote WebSocket transport for ACP agents and clients" [third-party]; acp-ws-bridge; `openclaw acp` forwards to OpenClaw's gateway over WebSocket [docs]) | Registry: `claude-acp`, `codex-acp`, `opencode`/`opencode-v2`, `muse-code`, `grok-build`, `gemini`, `cursor`, `github-copilot-cli`; `hermes acp` over stdio; no entry for Hermes, OpenClaw, dots [docs registry.json] |
| **A2A v1.0** | Not a wake primitive, but `SendMessage` to the agent's endpoint is wake + ask in one call **if the host is up and reachable**; no queueing for an offline agent (not stated). Push goes the other way: "When a task update occurs, the agent sends an HTTP POST request to the configured webhook URL." [docs] | **Yes**: `SendMessage`, `SendStreamingMessage` (SSE), `SubscribeToTask`. Self-description is standard: Agent Card at `/.well-known/agent-card.json` with required `name, description, supportedInterfaces, version, capabilities, defaultInputModes, defaultOutputModes, skills` [docs §8.2] | JSON-RPC/HTTP, gRPC, HTTP+JSON | an HTTP server exposing the endpoint and card | **OpenClaw** `@openclaw/a2a` (`/a2a/v1`, card, inbound SendMessage/GetTask, no push, no streaming, "Active work is not resumed after a restart") [docs]; **Hermes** A2A "works in both directions", SSE streaming, HMAC push, "No token ⇒ localhost only." [docs]; Claude Code/Codex/OpenCode/Grok/dots/Muse: not found |
| Host-native APIs | only if the host is running | OpenCode `opencode serve` `POST /session/:id/message` "Send a message and wait for response" [docs]; Codex `app-server` stdio JSONL, WebSocket "experimental and unsupported" (`--listen ws://`, "only for localhost or an SSH port-forwarded connection") [docs]; Claude Code channels: "A channel is an MCP server that pushes events into a Claude Code session", stdio, research preview; a 2026-07-28 channel "can't deliver channel messages"; under `-p`/Agent SDK "Claude Code ignores the flag and the channel doesn't register" [docs] | per host | the agent CLI itself, up | per host |
| **AWP — Agent Wake Protocol** (github.com/Manifestro/awp) | **Yes**: "Wake an AI agent when something happens outside its current session."; "AWP solves the return path with an outbound WebSocket connection."; "one-shot, isolated invocation of the registered runtime" (e.g. `codex exec resume`) [docs, verified] | No; one-way | outbound WebSocket, no inbound port | the AWP local client, always on | Codex and generic (Claude Code, any CLI) adapters; "early alpha"; provider self-hostable (FastAPI reference) [docs, verified] |
| Vendor triggers | Grok Bot routines: schedule, Slack events, GitHub/Linear/Sentry/PagerDuty, "a webhook call" with `Authorization: Bearer <key>`; a 200 "accepted the call and started a run" [docs]. dots: "has no webhooks or routines", woken by MCP Events only [third-party Ando] | No; Grok "check the Bot's chat for the result" | HTTPS POST | vendor cloud | Grok Bot; dots via ChatGPT MCP Events |
| Others | IBM ACP merged into A2A, archived 2025-08-27 [docs]. NLIP (Ecma-430/431/432/433, 2025-12-10) request/response over HTTP/WS/AMQP; offline wake not stated. ANP/AIP (IETF draft) best-effort datagrams, no store-and-forward. Agora, AP2: not relevant | | | | |

### 9.2 Generic lightweight buses

| Mechanism | Agent host must run | Offline durability | Bidirectional | Notes |
|---|---|---|---|---|
| **ntfy** | nothing persistent: `curl ".../topic/json?poll=1&since=<id>"`; or a long-lived `/json`, `/sse`, `/ws` stream | "By default, ntfy keeps messages in-memory for 12 hours" (persistent with `cache-file`/`database-url`); `since=` "Return cached messages since timestamp, duration or message ID" [docs] | not a feature; a subscriber can publish to another topic | publish via HTTP PUT/POST; scheduled delivery 10 s–3 days; e-mail forwarding (`X-Email`); publish via e-mail; UnifiedPush; self-host `ntfy serve`; ACLs and tokens |
| MQTT | persistent client or durable session (clean session=false) | QoS≥1 messages "stored until it connects again"; retained messages [docs] | yes | Mosquitto/EMQX self-host |
| Matrix as bus | long-poll `/sync` with `since`/`next_batch`, no inbound port | yes (server log; "limited" timeline + `/messages` backfill) | yes | homeserver self-host |
| Nostr relay (Buzz) | WebSocket client with keypair | per relay policy; Buzz backfills 24 h [docs] | yes | |
| smee.io / Hookdeck / ngrok | a client process or tunnel | smee: "Webhook payloads are never stored on the server" [docs]; Hookdeck retries up to 50 | tunnel: inherent | relays for agents that can expose nothing |

### 9.3 Per-agent: what a third party can ring, and whether an answer comes back the same way

| Agent | Inbound that starts a turn | Latency / floor | Answer in the HTTP response? | User background process on its VM |
|---|---|---|---|---|
| OpenClaw | `POST /hooks/wake`, `/hooks/agent` (Bearer / `x-openclaw-token`; disabled by default) [docs]; A2A `SendMessage`; MCP Events plugin | immediate; cron floor 30 s | hooks: **no** — "The response never includes model output or summaries." (even with `waitForCompletion`). A2A: **yes** ("A completed response contains a task with the reply in its artifact") [docs] | self-hosted |
| Hermes | HMAC-signed webhooks (agent / deliver_only / cron_job / coalesce) [docs]; A2A | immediate; cron tick 60 s | webhook agent mode: `202 accepted`, output to `deliver` [src]. A2A: yes (`A2A_REPLY_TIMEOUT` 300 s) [docs] | self-hosted |
| Grok Bot | routine triggers: webhook (Bearer), email to `mail.grokbot.com` ("Anyone can send mail to the address."; "New mail doesn't start a chat or wake a Bot on its own." — needs a routine), Slack mention/phrase [docs] | event triggers: no floor stated; caps 300 runs/h total, 200/h per routine; schedules ≥5 min | **no** — "check the Bot's chat for the result"; Slack post needs approval, email waits as draft [docs] | "Every Bot on your account uses one persistent cloud computer."; processes/idle: not stated |
| dots | Slack DM/mention or owner-configured monitoring task; MCP Events `agent.wake` via ChatGPT plugin connection [docs, third-party] | not stated | no HTTP path; "By default, your dot responds to you in Slack." | "has its own computer and browser"; processes: not stated |
| Claude Duet | none documented; "checks your connected accounts every few hours" [marketing]; sibling Claude Code scheduled tasks have an API trigger (≥1 h cron) with no stated link to Duet [docs] | hours | n/a | VM; processes: not stated |
| Muse | none documented; platform.muse.ai is a connector directory; recurring tasks exist, interval not stated | not stated | n/a | "persistent, isolated Linux computer"; processes: not stated. Observed: one Muse agent runs a long-lived WebSocket client with owner-built auto-restart (container restarts occasionally) |
| Instinct | nothing documented | | | |
| Claude Code / Codex / OpenCode (CLI) | only via something on their host: ACP bridge, AWP local client, OpenCode `serve`, Codex `app-server` | immediate | ACP prompt turn: yes; OpenCode `POST /session/:id/message`: yes | n/a |

### 9.4 How systems obtain an agent's profile

| System | Source | Out-of-band fetch | Agent-refreshable | Used for routing |
|---|---|---|---|---|
| Ando | owner-edited `name`/`role` in Settings; agent may send `description` (≤280) in its connect request; also "introduces itself" in chat | `GET /agents/{id}` (`display_name`, `role_name`) | **no update API** (full OpenAPI read); owner edits in UI | **yes**: the participation judge "weighs the agent's role, description, rules and the channel's proactivity." |
| Agent Room | agent self-picks `name` only | `GET /v1/network-agents/me` | no | no; mention/reply rules only |
| Aurmada | register `name` (≤60) + `description` (≤280, required); publishes an A2A card; directory "We fetch the card or run the MCP handshake" | public `GET /api/v1/agents/HANDLE/profile` | no self PATCH | by @handle |
| Conduyt | owner names the teammate; no description | `agent list` | no | mention/thread only |
| Moltbook | register `name`, `description` | yes | **yes**: `PATCH /api/v1/agents/me` | n/a |
| A2A Agent Card | agent-hosted JSON | yes by design | yes (republish) | consumer's choice |
| Platforms | Mattermost bot `description` (`POST/PUT /api/v4/bots`); Rocket.Chat `bio`/`statusText`; Slack manifest `description`/`long_description`; Discord app `description`; Matrix custom profile field `PUT /profile/{userId}/{keyName}` (≤64 KiB total) [docs] | REST | owner or agent | none route on them |

Asking an agent to describe itself and getting the text back works over A2A (OpenClaw, Hermes), ACP (prompt turn), probably OpenClaw `/v1/chat/completions`; it does not work over OpenClaw `/hooks/agent`, Grok routine webhooks, Hermes agent-mode webhooks, dots, Muse. MCP `clientInfo` (`Implementation`) carries an optional static `description` ("self-reported … intended for display, logging, and debugging").

### 9.5 Findings

1. MCP core cannot wake and no longer carries a server→client question; a monitor that is an MCP server hears from the agent only when the agent calls it.
2. MCP Events (SEP-3415) is the one standard-track push into hosted agents, webhook mode only, accepted but unfinished; shipped by ChatGPT (incl. dots), OpenClaw, OpenCode (experimental); not by Claude Code, Codex, Hermes. It needs the monitor to be a public HTTPS endpoint, and delivery retries are bounded.
3. A2A is the only cross-agent protocol with wake + ask-and-answer in one call plus a standard self-description; OpenClaw and Hermes serve it today; the agent host must be up and reachable.
4. ACP points the other way (IDE spawns agent over stdio); remote use is bridges that dial out from the agent host. AWP is prior art for exactly "a tiny always-on local client with an outbound WebSocket that runs a one-shot turn of a CLI agent" (alpha).
5. No doorbell of any vendor returns the agent's answer in the HTTP response; answers come back through a channel the agent writes to.
6. Profiles: the mature split is owner-written profile on the platform used by a server-side judge (Ando) vs mention-only routing with a display name (the rest). The only machine-readable, agent-refreshable format in use is the A2A Agent Card.

Sources read for this section (Ando docs and OpenAPI; modelcontextprotocol.io 2026-07-28 spec, SEP-3415 PR, triggers-events charter; developers.openai.com/plugins/build/mcp-events.md; agentclientprotocol.com transports/RFDs/registry; a2a-protocol.org specification; docs.openclaw.ai a2a/mcp-events/webhooks/openai-http-api; hermes-agent.nousresearch.com a2a/webhooks/cron; docs.x.ai grok-bot; learn.chatgpt.com dots; code.claude.com channels-reference/mcp; opencode.ai server/changelog v2.3.0; github.com/Manifestro/awp; docs.ntfy.sh; mosquitto/EMQX; spec.matrix.org; NIP-01; agentroom.chat/agents.txt; rooms.aurmada.com skill.md/openapi.json; moltbook.com/skill.md; Mattermost bots.yaml; Rocket.Chat/Slack/Discord/Matrix profile APIs).
