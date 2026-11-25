# AgentCoop Tool Instruction Index

This session uses lazy loading for detailed AgentCoop (previously known as agent-chat-gateway, or ACG) tool manuals. The full scheduling and fetch-history instructions are intentionally not injected by default.

When the user asks for one of these capabilities, load the matching instruction first and follow it exactly:

---
instruction: scheduling
detail: coop legacy instructions scheduling
when: User asks to create, list, pause, resume, or delete scheduled tasks, reminders, recurring jobs, or automations.
rule: Before running `coop legacy schedule ...`, run `coop legacy instructions scheduling` and follow the returned instructions exactly.
---

---
instruction: fetch-history
detail: coop legacy instructions fetch-history
when: You need to look up earlier room messages, refresh recent history, page through channel history, or fetch raw evidence from the conversation.
rule: Before running `coop legacy fetch-history ...`, run `coop legacy instructions fetch-history` and follow the returned instructions exactly.
---

Available command:

```bash
coop legacy instructions <scheduling|fetch-history>
```
