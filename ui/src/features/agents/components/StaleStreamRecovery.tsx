import { useEffect, useRef } from "react"
import { STREAM_CONTROLLER, useStreamContext } from "@langchain/react"

const REHYDRATE_DEBOUNCE_MS = 300

/**
 * Reconciles a client stream that outlived its own connection.
 *
 * The SDK's SSE transport does not reconnect once a custom `fetch` is in use
 * (the dashboard sends a session cookie), so a long model call or
 * summarization pass can outlive the stream: the run finishes server-side,
 * the 3s status poll reports it, but the browser never saw the terminal
 * lifecycle event — so `stream.isLoading` stays `true` and the final reply
 * is missing from the transcript until a manual refresh.
 *
 * When the polled run status flips `running` → settled while the stream is
 * still loading, re-hydrate the thread state once. Same-thread `hydrate()`
 * re-applies the checkpointed `values` (the final reply is checkpointed),
 * which is the same data a refresh would load.
 */
export function StaleStreamRecovery({
  threadId,
  serverRunning,
}: {
  threadId: string
  serverRunning: boolean
}) {
  const stream = useStreamContext()
  const controller = stream[STREAM_CONTROLLER]
  const previousRunningRef = useRef<boolean | null>(null)

  useEffect(() => {
    const previous = previousRunningRef.current
    previousRunningRef.current = serverRunning
    if (serverRunning || previous !== true) return
    if (!stream.isLoading) return

    let cancelled = false
    const timer = setTimeout(() => {
      if (cancelled) return
      void controller.hydrate(threadId).catch(() => undefined)
    }, REHYDRATE_DEBOUNCE_MS)
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [controller, serverRunning, stream.isLoading, threadId])

  return null
}
