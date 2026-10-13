import { describe, expect, it } from "vitest"

import { extractConversationSummary } from "./summaryEvent"

describe("extractConversationSummary", () => {
  it("returns null when there is no values snapshot", () => {
    expect(extractConversationSummary(undefined)).toBeNull()
    expect(extractConversationSummary(null)).toBeNull()
    expect(extractConversationSummary("nope")).toBeNull()
  })

  it("returns null when the private channel is absent (no compaction yet)", () => {
    expect(extractConversationSummary({ messages: [], files: {} })).toBeNull()
  })

  it("returns null when the event carries no usable summary text", () => {
    expect(
      extractConversationSummary({
        _summarization_event: { cutoff_index: 10 },
      })
    ).toBeNull()
    expect(
      extractConversationSummary({
        _summarization_event: {
          cutoff_index: 10,
          summary_message: { type: "human", content: "   " },
        },
      })
    ).toBeNull()
  })

  it("extracts text and offload path from a string-content summary", () => {
    const summary = extractConversationSummary({
      messages: [],
      _summarization_event: {
        cutoff_index: 636,
        file_path: "/conversation_history/session_abc.md",
        summary_message: {
          type: "human",
          content:
            "You are in the middle of a conversation...\n\n<summary>...</summary>",
        },
      },
    })
    expect(summary).toEqual({
      text: "You are in the middle of a conversation...\n\n<summary>...</summary>",
      filePath: "/conversation_history/session_abc.md",
    })
  })

  it("joins text blocks when the summary content is block-shaped", () => {
    const summary = extractConversationSummary({
      _summarization_event: {
        summary_message: {
          content: [
            { type: "text", text: "part one " },
            { type: "reasoning", reasoning: "skipped" },
            { type: "text", text: "part two" },
          ],
        },
      },
    })
    expect(summary?.text).toBe("part one part two")
    expect(summary?.filePath).toBeUndefined()
  })
})
