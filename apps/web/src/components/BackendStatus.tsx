import { useEffect, useState } from 'react'

import { api } from '../lib/api'

/** Which inference backends the API has registered. */
export function BackendStatus() {
  const [names, setNames] = useState<string[] | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let cancelled = false
    api
      .capabilities()
      .then((caps) => {
        if (!cancelled) setNames(caps.backends.map((b) => b.backend))
      })
      .catch(() => {
        if (!cancelled) setFailed(true)
      })
    return () => {
      cancelled = true
    }
  }, [])

  if (failed) return <span className="tag tag-error">api down</span>
  if (!names) return <span className="tag">…</span>

  return <span className="tag">{names.join(' · ') || 'no backends'}</span>
}
