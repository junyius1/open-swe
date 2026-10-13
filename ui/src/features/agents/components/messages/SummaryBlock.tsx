import { useState } from "react"
import { ChevronRight, FileText } from "lucide-react"

import { Markdown } from "@/features/agents/components/chat/Markdown"
import type { ConversationSummary } from "@/features/agents/lib/summaryEvent"

/**
 * Collapsible view over the conversation-compaction summary the deepagents
 * summarization middleware stashes in the private `_summarization_event`
 * channel. Styled to match the model's reasoning block ("Thought"): collapsed
 * to a single `> Summary` row by default, expandable on demand to reveal the
 * condensed summary. It is a transcript-level annotation, not a chat message.
 */
export function SummaryBlock({ summary }: { summary: ConversationSummary }) {
  const [expanded, setExpanded] = useState(false)
  if (!summary.text) return null

  return (
    <div className="my-1" data-testid="conversation-summary">
      <button
        type="button"
        onClick={() => setExpanded((value) => !value)}
        className="flex items-center gap-1 text-left transition-opacity hover:opacity-90"
        aria-expanded={expanded}
      >
        <ChevronRight
          className={`size-3 shrink-0 text-muted-foreground/65 transition-transform ${expanded ? "rotate-90" : ""}`}
          aria-hidden
        />
        <span className="text-[13px] text-muted-foreground">Summary</span>
      </button>
      {expanded && (
        <div className="ms-1 mt-1 border-s border-border/45 ps-3 text-[13px] leading-5 text-muted-foreground">
          <Markdown content={summary.text} />
          {summary.filePath && (
            <div className="mt-2 flex items-center gap-1.5 text-xs break-all text-muted-foreground/80">
              <FileText className="size-3 shrink-0" aria-hidden />
              <span>Full history: {summary.filePath}</span>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
