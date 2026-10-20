/**
 * Derive whether the agent is live from the two independent signals the UI
 * has: the server-reported run status (polled every few seconds) and the
 * client stream's loading flag (driven only by lifecycle SSE events).
 *
 * The SDK's SSE transport does not reconnect once a custom `fetch` is in use
 * (dashboard session cookie), so a long model call can outlive the stream:
 * the run finishes server-side while `stream.isLoading` stays stuck `true`
 * until a page refresh. When the polled server truth reports a non-running
 * status, it is authoritative and must win over the stale client flag,
 * otherwise the thread's stop button (and composer busy state) never clears.
 *
 * The inverse direction is intentionally kept: a server `running` status
 * always means live, and a non-running status with a live stream stays live
 * (optimistic sends set `status: "running"` locally, and a run started from
 * Slack/Linear/GitHub is joined by polling before its first lifecycle event
 * reaches this browser).
 */
export function isAgentRunLive(
  serverRunning: boolean | undefined,
  streamIsLoading: boolean
): boolean {
  if (serverRunning === false) return false
  return serverRunning === true || streamIsLoading
}
