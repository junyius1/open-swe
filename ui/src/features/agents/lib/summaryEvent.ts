/**
 * Extract the conversation-compaction summary the deepagents summarization
 * middleware stashes in the private `_summarization_event` state channel.
 *
 * The middleware is non-mutating: it never inserts the summary into the
 * `messages` channel, but this vendored langgraph checkpoint serializer does
 * not honor the channel's `OmitFromSchema(output=True)`, so the event leaks
 * into the served `values` snapshot (and thus the SDK's `stream.values`).
 * Rather than hide it server-side, the transcript surfaces it as a
 * collapsible "Summary" block (`SummaryBlock`) so nothing is lost from view.
 */
export interface ConversationSummary {
  /** The condensed summary text (the `summary_message` content). */
  text: string
  /** Sandbox path of the full offloaded history, when present. */
  filePath?: string
}

/** Key of the private channel the summarization middleware writes to. */
const SUMMARIZATION_EVENT_KEY = "_summarization_event"

function contentText(content: unknown): string {
  if (typeof content === "string") return content
  if (!Array.isArray(content)) return ""
  const parts: Array<string> = []
  for (const block of content) {
    if (!block || typeof block !== "object") continue
    const text = (block as Record<string, unknown>).text
    if (typeof text === "string" && text.trim()) parts.push(text.trim())
  }
  return parts.join(" ")
}

/**
 * Read the compaction summary out of a `values` snapshot (the SDK's
 * `stream.values` / the `/state` + `/history` response). Returns `null` when
 * the thread has never been compacted or the channel is malformed.
 */
export function extractConversationSummary(
  values: unknown
): ConversationSummary | null {
  if (!values || typeof values !== "object") return null
  const event = (values as Record<string, unknown>)[SUMMARIZATION_EVENT_KEY]
  if (!event || typeof event !== "object") return null
  const ev = event as Record<string, unknown>

  const message = ev.summary_message
  let text = ""
  if (typeof message === "string") {
    text = message
  } else if (message && typeof message === "object") {
    text = contentText((message as Record<string, unknown>).content)
  }
  text = text.trim()
  if (!text) return null

  const filePath =
    typeof ev.file_path === "string" && ev.file_path ? ev.file_path : undefined
  return { text, ...(filePath ? { filePath } : {}) }
}
