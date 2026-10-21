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

/** The topic set the socket was last asked to carry, sorted. */
let subscribed: string | null = null

/** A coalescing timer for topic-set changes. */
let pending: number | null = null
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
  // Named, not bare. The server's vocabulary is `kind:ident` and a name it does
  // not recognise is dropped — which is a silent failure, so the shape has to be
  // the one it expects rather than the one this side finds convenient.
  url.searchParams.set('topics', topics.join(','))
  return url.toString()
}

/**
 * What a client calls the viewer's own topic.
 *
 * It cannot name its folder — that is a hash of a cookie it does not read — so
 * it asks for `me` and the server resolves it. Which is also what stops it
 * naming anyone else's.
 */
export const VIEWER_TOPIC = 'viewer:me'

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

function accepted(message: Wire & { topics?: string[] }): void {
  // Asked for three, given none: the two sides disagree about what a topic is
  // called, and every symptom of that is the connection looking healthy. Said
  // out loud for the same reason a dropped vertex-label array is: silence and
  // success are otherwise indistinguishable.
  const wanted = watched.size
  const given = message.topics?.length ?? 0
  if (wanted > 0 && given === 0) {
    console.warn(
      `[stream] the server accepted none of the ${wanted} topic(s) asked for; ` +
        'progress will not arrive. Check the topic names against the route.',
    )
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
  opened.onmessage = (event) => {
    const raw = String(event.data)
    try {
      const parsed = JSON.parse(raw) as Wire & { topics?: string[] }
      if (parsed.type === 'ready') {
        accepted(parsed)
        return
      }
    } catch {
      // `handle` re-parses and ignores it; nothing here needs to care.
    }
    handle(raw)
  }
  opened.onerror = () => {
    // `onclose` always follows; that is where the retry is scheduled.
    opened.close()
  }
  opened.onclose = () => {
    live = false
    wentOffline()
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
 * Close a socket deliberately: clear `onclose` so it is not retried, and use 1000
 * so the dev proxy does not log `write ECONNABORTED` for a socket we dropped.
 */
function shut(target: WebSocket | null): void {
  if (!target) return
  target.onclose = null
  try {
    target.close(1000)
  } catch {
    // Already closing, or never opened. Nothing to do either way.
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
  shut(previous)
  open()
}

function signature(): string {
  return [...watched].sort().join(',')
}

/**
 * Bring the socket in line with `watched`, once per batch. Deferred so a render
 * that watches several topics reconnects once, and compared so a no-op change
 * does not tear down a good connection.
 */
function sync(): void {
  if (pending !== null) return
  pending = window.setTimeout(() => {
    pending = null
    const wanted = signature()

    if (watched.size === 0) {
      if (socket !== null || subscribed !== null) {
        subscribed = null
        const previous = socket
        socket = null
        live = false
        shut(previous)
      }
      return
    }

    if (wanted === subscribed && socket !== null) return
    subscribed = wanted
    restart()
  }, 0)
}

/** Follow a topic. Returns the function that stops following it. */
export function watchTopic(topic: string): () => void {
  watched.add(topic)
  sync()

  return () => {
    watched.delete(topic)
    sync()
  }
}

/** Follow a job. Returns the function that stops following it. */
export function watch(jobId: string): () => void {
  const stop = watchTopic(`job:${jobId}`)
  return () => {
    // Its resolver goes with it, or a page that navigates away mid-run leaves a
    // promise nobody will ever settle.
    awaitingSettle.delete(jobId)
    stop()
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

/**
 * Resolves once the socket is down — immediately if it already is.
 *
 * What lets the caller stop polling entirely while the stream is up. The
 * terminal update arrives on the socket, and `finish` leaves its value in place
 * on the server, so a client that reconnects after a drop is handed the ending
 * it missed rather than waiting for one that already happened.
 */
export function waitForOffline(): Promise<void> {
  if (!live) return Promise.resolve()
  return new Promise((resolve) => offline.push(resolve))
}

const offline: (() => void)[] = []

function wentOffline(): void {
  for (const resolve of offline.splice(0)) resolve()
}

export function subscribeProgress(listener: (progress: Progress) => void): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}
