import { describe, expect, it } from "vitest"

import { isAgentRunLive } from "./agentRunStatus"

describe("isAgentRunLive", () => {
  it("stays live while the stream reports loading, regardless of server truth", () => {
    expect(isAgentRunLive(undefined, true)).toBe(true)
    expect(isAgentRunLive(true, true)).toBe(true)
    expect(isAgentRunLive(false, true)).toBe(false)
  })

  it("is idle when neither the server nor the stream reports a run", () => {
    expect(isAgentRunLive(undefined, false)).toBe(false)
    expect(isAgentRunLive(false, false)).toBe(false)
  })

  it("treats a polled running status as live even before the stream joins it", () => {
    expect(isAgentRunLive(true, false)).toBe(true)
  })
})
