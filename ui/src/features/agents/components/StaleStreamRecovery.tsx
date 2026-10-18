import { useEffect, useRef } from "react"
import { STREAM_CONTROLLER, useStreamContext } from "@langchain/react"

import { agentsApi } from "../lib/api"

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
 * still loading, mark the controller idle (`stop` with `cancel: false`) and
 * re-hydrate the thread state once. `stop` clears the stuck `isLoading` flag
 * that the missed terminal event left behind (re-enabling the composer and
 * stopping the "Queued next" fallback), and same-thread `hydrate()`
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
      void (async () => {
        // Re-check the live status before acting: if a new run started in
        // the meantime the `stop` below must not abort its client-side
        // tracking.
        try {
          const thread = await agentsApi.getThread(threadId, {
            markViewed: false,
          })
          if (thread.status === "running") return
        } catch {
          return // network blip — leave the next poll's chance alone
        }
        // `hydrate` alone never clears `isLoading` — the flag is only
        // driven by root lifecycle events — so the composer stays
        // disabled and keeps queueing. `stop` marks the controller
        // idle; `cancel: false` keeps any server-side run untouched.
        await controller.stop({ cancel: false }).catch(() => undefined)
        await controller.hydrate(threadId).catch(() => undefined)
      })()
    }, REHYDRATE_DEBOUNCE_MS)
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [controller, serverRunning, stream.isLoading, threadId])

  return null
}
