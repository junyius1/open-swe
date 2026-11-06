import { useEffect, useRef } from "react"

import {
  STREAM_CONTROLLER,
  useStreamContext as useAgentThreadStream,
} from "@langchain/react"

import { agentsApi } from "../lib/api"

const STALE_STREAM_GRACE_MS = 1000
const STALE_STREAM_RETRY_MS = 3000

interface StaleStreamRecoveryProps {
  threadId: string
  serverRunning: boolean
  /**
   * Authoritative check that the run the client believes is live still is.
   * Defaults to a fresh cloud thread fetch; the desktop local view passes a
   * bridge-backed check because cloud fetches 404 for local thread ids.
   */
  confirmRunning?: () => Promise<boolean>
}

/**
 * Clears the client stream's loading flag when the thread has settled on the
 * server but no terminal stream event ever arrived (a broken SSE connection).
 *
 * This is a level check, not a transition: it heals states that were already
 * stuck before this fix was loaded, not only runs the component watched start.
 * A run that is actually live confirms through {@link confirmRunning} and the
 * check stands down; failed checks retry while the stuck condition persists.
 */
export function StaleStreamRecovery({
  threadId,
  serverRunning,
  confirmRunning,
}: StaleStreamRecoveryProps) {
  const stream = useAgentThreadStream()
  const controller = stream[STREAM_CONTROLLER]
  const confirmRef = useRef(confirmRunning)

  useEffect(() => {
    confirmRef.current = confirmRunning
  }, [confirmRunning])

  useEffect(() => {
    if (!stream.isLoading || serverRunning) return

    let timer: ReturnType<typeof setTimeout> | undefined
    let disposed = false

    const attempt = async () => {
      try {
        const confirm =
          confirmRef.current ??
          (async () =>
            (await agentsApi.getThread(threadId, { markViewed: false }))
              .status === "running")
        if (await confirm()) {
          if (!disposed) timer = setTimeout(attempt, STALE_STREAM_RETRY_MS)
          return
        }
      } catch {
        if (!disposed) timer = setTimeout(attempt, STALE_STREAM_RETRY_MS)
        return
      }
      if (disposed) return
      controller.stop({ cancel: false })
      await controller.hydrate(threadId)
    }

    timer = setTimeout(() => {
      void attempt()
    }, STALE_STREAM_GRACE_MS)

    return () => {
      disposed = true
      if (timer) clearTimeout(timer)
    }
  }, [controller, serverRunning, stream.isLoading, threadId])

  return null
}
