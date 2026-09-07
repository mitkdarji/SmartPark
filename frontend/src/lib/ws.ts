/**
 * WebSocket subscription with automatic reconnection.
 *
 * The token goes in the query string because the WebSocket API has no way to
 * set an Authorization header. Reconnection backs off exponentially and gives
 * up after a bounded number of attempts, so a dead backend does not turn into
 * an infinite reconnect loop hammering the server.
 */

import { getToken } from './api'
import type { LiveEvent } from './types'

const MAX_ATTEMPTS = 6
const BASE_DELAY_MS = 800

export interface Subscription {
  close: () => void
}

export function subscribe(
  channel: string,
  onEvent: (event: LiveEvent) => void,
  onStatus?: (status: 'connecting' | 'open' | 'closed') => void,
): Subscription {
  let socket: WebSocket | null = null
  let attempts = 0
  let closedByCaller = false
  let timer: number | undefined

  const connect = () => {
    if (closedByCaller) return
    const token = getToken()
    if (!token) return

    const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws'
    const url = `${protocol}://${window.location.host}/ws/${channel}?token=${encodeURIComponent(token)}`

    onStatus?.('connecting')
    socket = new WebSocket(url)

    socket.onopen = () => {
      attempts = 0
      onStatus?.('open')
      // A periodic ping keeps intermediaries from idling the socket out.
      const ping = window.setInterval(() => {
        if (socket?.readyState === WebSocket.OPEN) socket.send('ping')
        else window.clearInterval(ping)
      }, 25000)
    }

    socket.onmessage = (message) => {
      try {
        onEvent(JSON.parse(message.data) as LiveEvent)
      } catch {
        /* ignore malformed frames rather than tearing down the stream */
      }
    }

    socket.onclose = () => {
      onStatus?.('closed')
      if (closedByCaller || attempts >= MAX_ATTEMPTS) return
      const delay = BASE_DELAY_MS * 2 ** attempts
      attempts += 1
      timer = window.setTimeout(connect, delay)
    }

    socket.onerror = () => socket?.close()
  }

  connect()

  return {
    close: () => {
      closedByCaller = true
      if (timer) window.clearTimeout(timer)
      socket?.close()
    },
  }
}
