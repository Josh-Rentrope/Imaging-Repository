import { useEffect } from 'react'

import { sliceUrl } from '../lib/api'
import type { SourceImages, SourceSummary } from '../lib/types'
import type { WindowLevel } from './MainPanel'

/**
 * The pixels a source is made of, as a strip and a stage.
 *
 * Presentational: listing, index and contrast live in `MainPanel`, so the 3D view
 * and this cannot disagree about which image is selected.
 */
export function ImageView({
  source,
  listing,
  error,
  index,
  onIndex,
  windowLevel,
  onWindowLevel,
}: {
  source: SourceSummary | null
  listing: SourceImages | null
  error: string | null
  index: number
  onIndex: (index: number) => void
  windowLevel: WindowLevel | null
  onWindowLevel: (window: WindowLevel) => void
}) {
  // Arrow keys step through, which is how a slice stack is actually read.
  useEffect(() => {
    if (!listing || listing.count === 0) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'ArrowDown' || event.key === 'ArrowRight') {
        onIndex(Math.min(index + 1, listing.count - 1))
      } else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') {
        onIndex(Math.max(index - 1, 0))
      }
    }
    globalThis.addEventListener('keydown', onKey)
    return () => globalThis.removeEventListener('keydown', onKey)
  }, [listing, index, onIndex])

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

  const images = listing.images
  const many = images.length > 40
  const current = images[index]
  // The edited value when there is one, the listing's default otherwise.
  // Reading only the listing would leave the contrast inputs editing state that
  // nothing consults — they would appear to do nothing.
  const windowRange: WindowLevel | null = windowLevel ?? listing.window ?? null
  // Per image, and may be absent: a mammogram with no scale must not be measured on.
  const spacing = current?.spacing ?? null

  return (
    <div className="image-view">
      <ul className="image-list">
        {images.map((image) => (
          <li key={image.index}>
            <button
              className={`image-list-item${image.index === index ? ' active' : ''}`}
              onClick={() => onIndex(image.index)}
              title={image.name}
            >
              {/* A thumbnail per row would mean one request per slice, which for
                  a 250-slice series is 250 images to pick one. The strip shows
                  thumbnails only when the set is small enough to afford it. */}
              {!many && (
                <img
                  src={sliceUrl(listing.source_id, image.index, windowRange)}
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
          {listing.presentation === 'plane' ? (
            spacing ? (
              <span className="tag mono" title={`Pixel spacing from ${current?.spacing_source}`}>
                {(spacing[0] * 1000).toFixed(0)} x {(spacing[1] * 1000).toFixed(0)} µm/px
              </span>
            ) : (
              <span
                className="tag tag-warn"
                title="This file carries no pixel spacing, so its pixels have no known size"
              >
                scale unknown
              </span>
            )
          ) : null}
        </div>

        <img
          className="image-full"
          src={sliceUrl(listing.source_id, index, windowRange)}
          alt={current?.name ?? 'image'}
        />

        <div className="image-stage-foot">
          <button className="ghost" onClick={() => onIndex(Math.max(0, index - 1))} disabled={index === 0}>
            ‹ prev
          </button>
          <input
            type="range"
            min={0}
            max={Math.max(0, listing.count - 1)}
            value={index}
            onChange={(event) => onIndex(Number(event.target.value))}
          />
          <button
            className="ghost"
            onClick={() => onIndex(Math.min(listing.count - 1, index + 1))}
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
                  onWindowLevel({ ...windowRange, width: Number(event.target.value) })
                }
              />
              <span className="muted">level</span>
              <input
                className="number-input mono"
                type="number"
                value={windowRange.center ?? 0}
                onChange={(event) =>
                  onWindowLevel({ ...windowRange, center: Number(event.target.value) })
                }
              />
            </span>
          )}
        </div>
      </div>
    </div>
  )
}
