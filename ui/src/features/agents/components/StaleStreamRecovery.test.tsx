/** @vitest-environment jsdom */

import { act, cleanup, render } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { StaleStreamRecovery } from "./StaleStreamRecovery"

const mocks = vi.hoisted(() => ({
  controller: {
    hydrate: vi.fn().mockResolvedValue(undefined),
    stop: vi.fn().mockResolvedValue(undefined),
  },
  streamController: Symbol("stream-controller"),
  stream: {
    isLoading: true,
  },
  getThread: vi.fn().mockResolvedValue({ status: "idle" }),
}))

vi.mock("@langchain/react", () => ({
  STREAM_CONTROLLER: mocks.streamController,
  useStreamContext: () => ({
    ...mocks.stream,
    [mocks.streamController]: mocks.controller,
  }),
}))

vi.mock("../lib/api", () => ({
  agentsApi: {
    getThread: mocks.getThread,
  },
}))

beforeEach(() => {
  vi.useFakeTimers()
  mocks.controller.hydrate.mockClear()
  mocks.controller.stop.mockClear()
  mocks.getThread.mockClear()
  mocks.getThread.mockResolvedValue({ status: "idle" })
  mocks.stream.isLoading = true
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe("StaleStreamRecovery", () => {
  it("heals a thread that is already stuck when the view mounts settled", async () => {
    render(<StaleStreamRecovery threadId="thread-1" serverRunning={false} />)

    await act(async () => {
      await Promise.resolve()
      await vi.advanceTimersByTimeAsync(1000)
    })

    expect(mocks.getThread).toHaveBeenCalledWith("thread-1", { markViewed: false })
    expect(mocks.controller.stop).toHaveBeenCalledTimes(1)
    expect(mocks.controller.stop).toHaveBeenCalledWith({ cancel: false })
    expect(mocks.controller.hydrate).toHaveBeenCalledTimes(1)
    expect(mocks.controller.hydrate).toHaveBeenCalledWith("thread-1")
  })

  it("re-hydrates once when the polled status settles while the stream is still loading", async () => {
    const view = render(
      <StaleStreamRecovery threadId="thread-1" serverRunning={true} />
    )

    view.rerender(
      <StaleStreamRecovery threadId="thread-1" serverRunning={false} />
    )

    await act(async () => {
      await Promise.resolve()
      await vi.advanceTimersByTimeAsync(1000)
    })

    expect(mocks.controller.stop).toHaveBeenCalledTimes(1)
    expect(mocks.controller.stop).toHaveBeenCalledWith({ cancel: false })
    expect(mocks.controller.hydrate).toHaveBeenCalledTimes(1)
    expect(mocks.controller.hydrate).toHaveBeenCalledWith("thread-1")
  })

  it("does not stop or re-hydrate when the stream already settled", async () => {
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

    expect(mocks.controller.stop).not.toHaveBeenCalled()
    expect(mocks.controller.hydrate).not.toHaveBeenCalled()
  })

  it("skips recovery when a fresh status check says the thread is running again", async () => {
    mocks.getThread.mockResolvedValue({ status: "running" })
    const view = render(
      <StaleStreamRecovery threadId="thread-1" serverRunning={true} />
    )
    view.rerender(
      <StaleStreamRecovery threadId="thread-1" serverRunning={false} />
    )

    await act(async () => {
      await Promise.resolve()
      await vi.advanceTimersByTimeAsync(4000)
    })

    expect(mocks.controller.stop).not.toHaveBeenCalled()
    expect(mocks.controller.hydrate).not.toHaveBeenCalled()
    expect(mocks.getThread).toHaveBeenCalledTimes(2)
  })

  it("retries later when the confirmation fetch fails", async () => {
    mocks.getThread.mockRejectedValue(new Error("network"))
    render(<StaleStreamRecovery threadId="thread-1" serverRunning={false} />)

    await act(async () => {
      await Promise.resolve()
      await vi.advanceTimersByTimeAsync(4000)
    })

    expect(mocks.controller.stop).not.toHaveBeenCalled()
    expect(mocks.controller.hydrate).not.toHaveBeenCalled()
    expect(mocks.getThread).toHaveBeenCalledTimes(2)
  })

  it("uses a provided confirmRunning check instead of the cloud fetch", async () => {
    const confirmRunning = vi.fn().mockResolvedValue(false)
    render(
      <StaleStreamRecovery
        threadId="thread-1"
        serverRunning={false}
        confirmRunning={confirmRunning}
      />
    )

    await act(async () => {
      await Promise.resolve()
      await vi.advanceTimersByTimeAsync(1000)
    })

    expect(mocks.getThread).not.toHaveBeenCalled()
    expect(confirmRunning).toHaveBeenCalledTimes(1)
    expect(mocks.controller.stop).toHaveBeenCalledTimes(1)
    expect(mocks.controller.hydrate).toHaveBeenCalledWith("thread-1")
  })

  it("stands down while confirmRunning reports the run is live", async () => {
    const confirmRunning = vi.fn().mockResolvedValue(true)
    render(
      <StaleStreamRecovery
        threadId="thread-1"
        serverRunning={false}
        confirmRunning={confirmRunning}
      />
    )

    await act(async () => {
      await Promise.resolve()
      await vi.advanceTimersByTimeAsync(4000)
    })

    expect(mocks.controller.stop).not.toHaveBeenCalled()
    expect(mocks.controller.hydrate).not.toHaveBeenCalled()
    expect(confirmRunning).toHaveBeenCalledTimes(2)
  })
})
