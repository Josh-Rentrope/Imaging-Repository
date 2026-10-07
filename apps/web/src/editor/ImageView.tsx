import { useEffect, useState } from 'react'

import { API_BASE, api } from '../lib/api'
import type { SourceImages, SourceSummary } from '../lib/types'

/** Contrast setting. Named apart from the DOM's `Window`, which it would shadow. */
interface WindowLevel {
  center: number | null
  width: number | null
}

/**
 * Where a source's pixels come from.
 *
 * A sliced series comes off the assembled volume, so the slice strip and the 3D
 * view are guaranteed to be looking at the same data in the same order. An
 * uploaded photo is served as itself, because that is what feeds the
 * reconstruction and re-encoding it would only lose information.
 *
 * Built on `API_BASE` rather than a literal, because these are `<img src>` and
 * nothing else would catch it: the API being on another origin in the deployed
 * topology turns a hardcoded `/api` into a 404 on every slice, with no error a
 * component could report.
 */
function imageUrl(sourceId: string, index: number, window: WindowLevel | null): string {
  const base = `${API_BASE}/sources/${sourceId}/images/${index}`
  if (!window || (window.center === null && window.width === null)) return base
  const params = new URLSearchParams()
  if (window.center !== null) params.set('level', String(window.center))
  if (window.width !== null) params.set('window', String(window.width))
  return `${base}?${params}`
}

export function ImageView({ source }: { source: SourceSummary | null }) {
  const [listing, setListing] = useState<SourceImages | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [index, setIndex] = useState(0)
  const [window, setWindow] = useState<WindowLevel | null>(null)

  const sourceId = source?.source_id ?? null

  useEffect(() => {
    if (!sourceId) {
      setListing(null)
      return
    }
    let cancelled = false
    setError(null)
    setIndex(0)

    api
      .listImages(sourceId)
      .then((found) => {
        if (cancelled) return
        setListing(found)
        // Open in the middle: for a scan that is usually where the anatomy of
        // interest is, and for a photo set the order does not matter.
        setIndex(Math.floor((found.count - 1) / 2))
        setWindow(found.window ?? null)
      })
      .catch((cause: unknown) => {
        if (cancelled) return
        setListing(null)
        setError(cause instanceof Error ? cause.message : String(cause))
      })

    return () => {
      cancelled = true
    }
  }, [sourceId])

  const images = listing?.images ?? []
  const many = images.length > 40
  const current = images[index]

  // Arrow keys step through, which is how a slice stack is actually read.
  useEffect(() => {
    if (!listing || listing.count === 0) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'ArrowDown' || event.key === 'ArrowRight') {
        setIndex((value) => Math.min(value + 1, listing.count - 1))
      } else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') {
        setIndex((value) => Math.max(value - 1, 0))
      }
    }
    globalThis.addEventListener('keydown', onKey)
    return () => globalThis.removeEventListener('keydown', onKey)
  }, [listing])

  // The edited value when there is one, the series default otherwise. Reading
  // only the listing here would leave the contrast inputs editing state that
  // nothing consults — they would appear to do nothing.
  const windowRange: WindowLevel | null = window ?? listing?.window ?? null

  if (!source) {
    return <div className="empty-state">Select a source to see its images.</div>
  }
  if (error) {
    return <div className="empty-state error-text">{error}</div>
  }
  if (!listing) {
    return <div className="empty-state">Loading images…</div>
  }
  if (listing.count === 0) {
    return <div className="empty-state">This source has no images.</div>
  }

  return (
    <div className="image-view">
      <ul className="image-list">
        {images.map((image) => (
          <li key={image.index}>
            <button
              className={`image-list-item${image.index === index ? ' active' : ''}`}
              onClick={() => setIndex(image.index)}
              title={image.name}
            >
              {/* A thumbnail per row would mean one request per slice, which for
                  a 250-slice series is 250 images to pick one. The strip shows
                  thumbnails only when the set is small enough to afford it. */}
              {!many && (
                <img
                  src={imageUrl(listing.source_id, image.index, windowRange)}
                  alt=""
                  loading="lazy"
                />
              )}
              <span className="mono">{image.name}</span>
            </button>
          </li>
        ))}
      </ul>

      <div className="image-stage">
        <div className="image-stage-head">
          <span className="tag">{current?.name}</span>
          <span className="tag mono">
            {index + 1} / {listing.count}
          </span>
          {listing.slice_spacing_mm ? (
            <span className="tag mono">{listing.slice_spacing_mm.toFixed(2)} mm apart</span>
          ) : null}
        </div>

        <img
          className="image-full"
          src={imageUrl(listing.source_id, index, windowRange)}
          alt={current?.name ?? 'image'}
        />

        <div className="image-stage-foot">
          <button
            className="ghost"
            onClick={() => setIndex((value) => Math.max(0, value - 1))}
            disabled={index === 0}
          >
            ‹ prev
          </button>
          <input
            type="range"
            min={0}
            max={Math.max(0, listing.count - 1)}
            value={index}
            onChange={(event) => setIndex(Number(event.target.value))}
          />
          <button
            className="ghost"
            onClick={() => setIndex((value) => Math.min(listing.count - 1, value + 1))}
            disabled={index >= listing.count - 1}
          >
            next ›
          </button>

          {windowRange && (
            <span className="image-window">
              <span className="muted">window</span>
              <input
                className="number-input mono"
                type="number"
                value={windowRange.width ?? 0}
                onChange={(event) =>
                  setWindow({ ...windowRange, width: Number(event.target.value) })
                }
              />
              <span className="muted">level</span>
              <input
                className="number-input mono"
                type="number"
                value={windowRange.center ?? 0}
                onChange={(event) =>
                  setWindow({ ...windowRange, center: Number(event.target.value) })
                }
              />
            </span>
          )}
        </div>
      </div>
    </div>
  )
}
