/** @vitest-environment jsdom */

import { cleanup, render, screen, fireEvent } from "@testing-library/react"
import { afterEach, describe, expect, it } from "vitest"

import { SummaryBlock } from "./SummaryBlock"
import type { ConversationSummary } from "@/features/agents/lib/summaryEvent"

const summary: ConversationSummary = {
  text: "## SESSION INTENT\n\nDo the thing.",
  filePath: "/conversation_history/session_abc.md",
}

afterEach(() => cleanup())

describe("SummaryBlock", () => {
  it("renders collapsed by default and shows only the Summary label", () => {
    render(<SummaryBlock summary={summary} />)
    const toggle = screen.getByRole("button", { name: /Summary/ })
    expect(toggle.getAttribute("aria-expanded")).toBe("false")
    expect(screen.queryByText(/Full history:/)).toBeNull()
  })

  it("expands on click to reveal the summary body and offload path", () => {
    render(<SummaryBlock summary={summary} />)
    fireEvent.click(screen.getByRole("button", { name: /Summary/ }))
    const toggle = screen.getByRole("button", { name: /Summary/ })
    expect(toggle.getAttribute("aria-expanded")).toBe("true")
    expect(screen.getByText(/Full history: /)).toBeTruthy()
  })

  it("renders nothing when the summary text is empty", () => {
    const { container } = render(<SummaryBlock summary={{ text: "" }} />)
    expect(container.firstChild).toBeNull()
  })
})
