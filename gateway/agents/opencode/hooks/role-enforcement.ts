/**
 * RC Gateway role enforcement plugin.
 *
 * Fires before every opencode tool call.
 * COOP_ROLE unset (local CLI) or "owner" → full access.
 * COOP_ROLE="guest" → enforce COOP_ALLOWED_TOOLS whitelist.
 *
 * COOP_ALLOWED_TOOLS: comma-separated tool name patterns.
 * Supports * wildcard suffix (e.g. "mcp__rocketchat__*").
 * If COOP_ALLOWED_TOOLS is empty, all tools are blocked for guests.
 *
 * For owner sessions, sensitive write/exec tools require human-in-the-loop
 * approval via the RC chat gateway.  COOP_APPROVAL_TOOLS lists the tool
 * patterns that trigger the opencode built-in permission.ask flow.
 * The gateway's OpenCodePermissionBroker listens for the resulting
 * permission.asked SSE event and posts an approval request to RC chat.
 */

/** Tools that require owner approval when running via the RC gateway. */
const DEFAULT_APPROVAL_TOOLS = ["bash", "write", "edit", "multiedit"]

export default function () {
  return {
    "tool.execute.before": async (
      input: { tool: string; sessionID: string; callID: string },
      output: unknown,
    ) => {
      const role = process.env.COOP_ROLE

      // ── Guest enforcement ────────────────────────────────────────────────
      if (role === "guest") {
        const allowed = (process.env.COOP_ALLOWED_TOOLS ?? "")
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean)

        const toolLower = input.tool.toLowerCase()
        const permitted = allowed.some((pattern) =>
          pattern.endsWith("*")
            ? toolLower.startsWith(pattern.slice(0, -1))
            : toolLower === pattern
        )

        if (!permitted) {
          throw new Error(`Guest: tool '${input.tool}' not in COOP_ALLOWED_TOOLS`)
        }
        return
      }

      // ── Owner: human-in-the-loop approval for sensitive tools ────────────
      // Only active when running via the RC gateway (COOP_ROLE=owner).
      // COOP_APPROVAL_TOOLS overrides the default list if set.
      if (role !== "owner") return  // local CLI (role unset) → no approval needed

      const approvalPatterns = process.env.COOP_APPROVAL_TOOLS
        ? process.env.COOP_APPROVAL_TOOLS.split(",").map((s) => s.trim().toLowerCase()).filter(Boolean)
        : DEFAULT_APPROVAL_TOOLS

      const toolLower = input.tool.toLowerCase()
      const needsApproval = approvalPatterns.some((pattern) =>
        pattern.endsWith("*")
          ? toolLower.startsWith(pattern.slice(0, -1))
          : toolLower === pattern
      )

      if (needsApproval) {
        // Setting output.status = "ask" triggers opencode's built-in
        // permission.asked SSE event, which the OpenCodePermissionBroker
        // listens to and forwards as an RC approval request.
        (output as { status?: string }).status = "ask"
      }
    },
  }
}
