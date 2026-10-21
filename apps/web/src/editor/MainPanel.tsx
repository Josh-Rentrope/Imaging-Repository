import { useEffect, useRef, useState } from 'react'

import { api, sliceUrl } from '../lib/api'
import type { SourceImages, SourceSummary, VolumePayload } from '../lib/types'
import { ImageView } from './ImageView'
import type { ResultView } from './ResultsSection'
import type { ScenePlane } from './Scene'
import { Viewport3D } from './Viewport3D'

const TABS = [
  { id: '3d', label: '3D View' },
  { id: 'images', label: 'Image View' },
] as const

type TabId = (typeof TABS)[number]['id']

/** Whether a result has geometry the 3D view can draw. */
function drawsIn3d(results: ResultView[]): boolean {
  return results.some((result) =>
    (result.job.result?.artifacts ?? []).some(
      (artifact) =>
        (artifact.kind === 'mesh' || artifact.kind === 'splat') &&
        (artifact.format === 'ply' || artifact.format === 'stl'),
    ),
  )
}

/** Contrast setting. Named apart from the DOM's `Window`, which it would shadow. */
export interface WindowLevel {
  center: number | null
  width: number | null
}

/**
 * The main panel: the geometry on one tab, the pixels it came from on the other.
 *
 * Both are views of the same source rather than two screens, so they share the
 * source selection above and the results below — switching tabs is changing
 * your mind about how to look, not about what you are looking at.
 *
 * The listing lives here because both tabs use it, and the tabs unmount each
 * other, so neither can hold the selected image itself.
 */
export function MainPanel({
  source,
  volume,
  results,
  busy,
  uploading,
}: {
  source: SourceSummary | null
  volume: VolumePayload | null
  results: ResultView[]
  busy: boolean
  uploading: boolean
}) {
  const [tab, setTab] = useState<TabId>('3d')
  const [listing, setListing] = useState<SourceImages | null>(null)
  const [imagesError, setImagesError] = useState<string | null>(null)
  const [index, setIndex] = useState(0)
  const [windowLevel, setWindowLevel] = useState<WindowLevel | null>(null)

  const sourceId = source?.source_id ?? null

  // Only a tab we chose may be taken back; a tab the user picked is theirs.
  const autoSwitched = useRef(false)

  // A source that is not one acquisition opens on its images.
  useEffect(() => {
    autoSwitched.current = false
    if (!sourceId) return
    if (source?.coherent === false) {
      setTab('images')
      autoSwitched.current = true
    } else {
      setTab('3d')
    }
    // Only on `sourceId`: reacting to `coherent` would undo a user's choice.
  }, [sourceId])

  // Once something is built from it, back to the geometry — if the switch was ours.
  const has3d = volume !== null || drawsIn3d(results)
  useEffect(() => {
    if (!autoSwitched.current || !has3d) return
    autoSwitched.current = false
    setTab('3d')
  }, [has3d, sourceId])

  const chooseTab = (next: TabId) => {
    // A tab the user picked is theirs; nothing switches it back.
    autoSwitched.current = false
    setTab(next)
  }

  useEffect(() => {
    if (!sourceId) {
      setListing(null)
      return
    }
    let cancelled = false
    setImagesError(null)

    api
      .listImages(sourceId)
      .then((found) => {
        if (cancelled) return
        setListing(found)
        // A scan opens in the middle; a set of projections has no middle.
        setIndex(found.presentation === 'plane' ? 0 : Math.floor((found.count - 1) / 2))
        setWindowLevel(
          found.window ?? {
            center: found.images[0]?.window_center ?? null,
            width: found.images[0]?.window_width ?? null,
          },
        )
      })
      .catch((cause: unknown) => {
        if (cancelled) return
        setListing(null)
        setImagesError(cause instanceof Error ? cause.message : String(cause))
      })

    return () => {
      cancelled = true
    }
  }, [sourceId])

  // The plane the 3D view draws. Geometry is per image: projections share nothing.
  const current = listing?.images[index]
  const plane: ScenePlane | null =
    source && listing?.presentation === 'plane' && current
      ? {
          url: sliceUrl(source.source_id, index, windowLevel),
          rows: current.rows ?? 0,
          columns: current.columns ?? 0,
          spacing: current.spacing ?? null,
          origin: current.origin ?? [0, 0, 0],
          direction: current.direction ?? [1, 0, 0, 0, 1, 0, 0, 0, 1],
          key: `${source.source_id}|${index}|${windowLevel?.center ?? ''}|${windowLevel?.width ?? ''}`,
        }
      : null

  return (
    <section className="viewport">
      <div className="view-tabs" role="tablist">
        {TABS.map((entry) => (
          <button
            key={entry.id}
            role="tab"
            aria-selected={tab === entry.id}
            className={`view-tab${tab === entry.id ? ' active' : ''}`}
            onClick={() => chooseTab(entry.id)}
          >
            {entry.label}
          </button>
        ))}
      </div>

      {tab === '3d' ? (
        <Viewport3D
          source={source}
          volume={volume}
          plane={plane}
          results={results}
          busy={busy}
          uploading={uploading}
        />
      ) : (
        <div className="view-pane">
          {/* Say why: an unexplained switch reads as the 3D view being broken. */}
          {source && !source.coherent && source.coherence ? (
            <p className="view-note">
              Opened as images: {source.coherence.reasons.join('; ')}.
            </p>
          ) : null}
          <ImageView
            source={source}
            listing={listing}
            error={imagesError}
            index={index}
            onIndex={setIndex}
            windowLevel={windowLevel}
            onWindowLevel={setWindowLevel}
          />
        </div>
      )}
    </section>
  )
}
