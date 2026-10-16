/** @vitest-environment jsdom */

import { act, cleanup, render } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { StaleStreamRecovery } from "./StaleStreamRecovery"

const mocks = vi.hoisted(() => ({
  controller: { hydrate: vi.fn().mockResolvedValue(undefined) },
  streamController: Symbol("stream-controller"),
  stream: {
    isLoading: true,
  },
}))

vi.mock("@langchain/react", () => ({
  STREAM_CONTROLLER: mocks.streamController,
  useStreamContext: () => ({
    ...mocks.stream,
    [mocks.streamController]: mocks.controller,
  }),
}))

beforeEach(() => {
  vi.useFakeTimers()
  mocks.controller.hydrate.mockClear()
  mocks.stream.isLoading = true
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe("StaleStreamRecovery", () => {
  it("re-hydrates once when the polled status settles while the stream is still loading", async () => {
    const view = render(
      <StaleStreamRecovery threadId="thread-1" serverRunning={true} />
    )

    view.rerender(
      <StaleStreamRecovery threadId="thread-1" serverRunning={false} />
    )

    await act(async () => {
      await Promise.resolve()
      await vi.advanceTimersByTimeAsync(300)
    })

    expect(mocks.controller.hydrate).toHaveBeenCalledTimes(1)
    expect(mocks.controller.hydrate).toHaveBeenCalledWith("thread-1")
  })

  it("does not re-hydrate on repeated settled renders", async () => {
    const view = render(
      <StaleStreamRecovery threadId="thread-1" serverRunning={true} />
    )
    view.rerender(
      <StaleStreamRecovery threadId="thread-1" serverRunning={false} />
    )
    view.rerender(
      <StaleStreamRecovery threadId="thread-1" serverRunning={false} />
    )

    await act(async () => {
      await vi.runAllTimersAsync()
    })

    expect(mocks.controller.hydrate).toHaveBeenCalledTimes(1)
  })

  it("does not re-hydrate when the stream already settled", async () => {
    mocks.stream.isLoading = false
    const view = render(
      <StaleStreamRecovery threadId="thread-1" serverRunning={true} />
    )
    view.rerender(
      <StaleStreamRecovery threadId="thread-1" serverRunning={false} />
    )

    await act(async () => {
      await vi.runAllTimersAsync()
    })

    expect(mocks.controller.hydrate).not.toHaveBeenCalled()
  })

  it("does not re-hydrate for a thread that was never running", async () => {
    render(<StaleStreamRecovery threadId="thread-1" serverRunning={false} />)

    await act(async () => {
      await vi.runAllTimersAsync()
    })

    expect(mocks.controller.hydrate).not.toHaveBeenCalled()
  })
})
