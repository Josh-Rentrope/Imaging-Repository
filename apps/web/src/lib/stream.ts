/**
 * The live event stream.
 *
 * One socket for the page, subscribed to the jobs it is currently following.
 * Progress arrives as it happens. A job *ending* is a hint and nothing more:
 * the caller still reads the job record once, because the record is the durable
 * truth and the stream is only a notification that it is time to read it.
 *
 * **The stream is never load-bearing.** A socket that will not open, is blocked
 * by a proxy, or dies mid-job leaves the caller polling — which is what the app
 * did before any of this existed. Nothing here surfaces an error for that:
 * a fallback that announces itself is worse than the fallback.
 */

import { API_BASE } from './api'

/** A progress value, as the server sends it. */
interface Wire {
  type: string
  task_id?: string
  label?: string
  stage?: string | null
  done?: number | null
  total?: number | null
  fraction?: number | null
  status?: string
}

export interface Progress {
  taskId: string
  label: string
  stage: string | null
  done: number | null
  total: number | null
  /** Null when the step cannot know how many there are. Draw a spinner, not a bar. */
  fraction: number | null
  status: string
}

/** Statuses that mean the job is still going. */
const RUNNING = new Set(['queued', 'running'])

/** How long to wait before trying a socket that just closed. */
const RETRY_MS = 4000

let socket: WebSocket | null = null
let live = false
let retry: number | null = null
/** Job ids the page is following. The socket is restarted when this changes. */
const watched = new Set<string>()
const awaitingSettle = new Map<string, Set<() => void>>()
const listeners = new Set<(progress: Progress) => void>()

/**
 * Where the socket lives.
 *
 * Derived from the same base the HTTP client uses, so the two cannot disagree
 * about which server they are talking to. `API_BASE` is relative in dev (the
 * Vite proxy rewrites `/api` away) and absolute in a deployment, so a relative
 * one is resolved against the page.
 */
export function streamUrl(topics: string[]): string {
  const base = API_BASE.startsWith('http') ? API_BASE : `${window.location.origin}${API_BASE}`
  const url = new URL(`${base}/ws`)
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  url.searchParams.set('topics', topics.join(','))
  return url.toString()
}

function send(progress: Progress): void {
  for (const listener of listeners) listener(progress)
}

function handle(raw: string): void {
  let message: Wire
  try {
    message = JSON.parse(raw) as Wire
  } catch {
    return
  }
  if (message.type !== 'progress' || !message.task_id) return

  const progress: Progress = {
    taskId: message.task_id,
    label: message.label ?? '',
    stage: message.stage ?? null,
    done: message.done ?? null,
    total: message.total ?? null,
    fraction: message.fraction ?? null,
    status: message.status ?? 'running',
  }
  send(progress)

  if (!RUNNING.has(progress.status)) {
    const waiting = awaitingSettle.get(progress.taskId)
    awaitingSettle.delete(progress.taskId)
    for (const resolve of waiting ?? []) resolve()
  }
}

function open(): void {
  if (socket || watched.size === 0) return
  if (retry !== null) {
    window.clearTimeout(retry)
    retry = null
  }

  let opened: WebSocket
  try {
    opened = new WebSocket(streamUrl([...watched]))
  } catch {
    // A malformed base, or a browser that will not have one. Polling covers it.
    return
  }
  socket = opened

  opened.onopen = () => {
    live = true
  }
  opened.onmessage = (event) => handle(String(event.data))
  opened.onerror = () => {
    // `onclose` always follows; that is where the retry is scheduled.
    opened.close()
  }
  opened.onclose = () => {
    live = false
    if (socket === opened) socket = null
    // Only while something is still being followed: a page with no jobs in
    // flight should not hold a socket open, nor retry one.
    if (watched.size > 0) {
      retry = window.setTimeout(() => {
        retry = null
        open()
      }, RETRY_MS)
    }
  }
}

/**
 * Restart the socket with the current topic set.
 *
 * A reconnect rather than a subscribe message, because the topic set is fixed
 * when the socket opens. Job ids are cheap to name again, and the server sends
 * the current value of every topic on connect — so a client that reconnects
 * mid-job is told where things are rather than waiting for the next change.
 */
function restart(): void {
  const previous = socket
  socket = null
  live = false
  if (previous) {
    previous.onclose = null
    previous.close()
  }
  open()
}

/** Follow a job. Returns the function that stops following it. */
export function watch(jobId: string): () => void {
  watched.add(jobId)
  if (socket) restart()
  else open()

  return () => {
    watched.delete(jobId)
    awaitingSettle.delete(jobId)
    if (watched.size === 0) {
      const previous = socket
      socket = null
      live = false
      if (previous) {
        previous.onclose = null
        previous.close()
      }
    } else {
      restart()
    }
  }
}

/**
 * Resolves when the job reports a status that is no longer running.
 *
 * Never rejects, and never resolves for a job that never ends — the caller
 * races it against polling rather than waiting on it alone.
 */
export function settled(jobId: string): Promise<void> {
  return new Promise((resolve) => {
    const waiting = awaitingSettle.get(jobId) ?? new Set()
    waiting.add(resolve)
    awaitingSettle.set(jobId, waiting)
  })
}

/** Whether the socket is currently open. What the poll interval keys off. */
export function isLive(): boolean {
  return live
}

export function subscribeProgress(listener: (progress: Progress) => void): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}
