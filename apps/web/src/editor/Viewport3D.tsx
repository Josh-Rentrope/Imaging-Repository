import { lazy, Suspense, useEffect, useMemo, useRef, useState, type RefObject } from 'react'

import type { ResultEnvelope, SourceSummary, VolumePayload } from '../lib/types'
import type { ResultView } from './ResultsSection'
import type { SceneModel, SceneSelection, SceneSurface } from './Scene'
import { ViewportControls } from './ViewportControls'
import { DEFAULT_SETTINGS, type ViewportSettings } from './viewportSettings'

// vtk.js is heavy and is only needed once something is actually being rendered,
// so the whole renderer is behind a dynamic import.
const Scene = lazy(() => import('./Scene').then((module) => ({ default: module.Scene })))

const MESH_FORMATS = new Set(['ply', 'stl'])

/**
 * Scale indicator. When `verified` is false the geometry has no metric anchor,
 * so measurements are suppressed rather than reported in invented millimetres.
 */
function ScaleTag({ scale }: { scale: ResultEnvelope['scale'] }) {
  if (scale.verified) {
    return (
      <span className="tag tag-ok" title={`Metric scale from ${scale.source}`}>
        mm
      </span>
    )
  }
  return (
    <span className="tag tag-warn" title="No depth and no fiducial: arbitrary units">
      arbitrary units
    </span>
  )
}

function placeholderFor(source: SourceSummary | null): string {
  if (!source) return 'Drop a source to begin'
  if (!source.renderable) return source.render_reason ?? 'Nothing to display'
  return 'Select source and run the pipeline'
}

// Fixed rather than fitted to the content, so the box can be placed exactly:
// a width that depended on the label would move the popup as it changed, and
// the flip below could only guess at the edge.
const POPUP_WIDTH = 200
const POPUP_OFFSET = 14
const POPUP_MARGIN = 8
// Two short lines plus padding. Only used to keep the popup off the top and
// bottom edges, so being a few pixels out is not visible.
const POPUP_HEIGHT = 52

function PickPopup({
  selection,
  viewport,
}: {
  selection: SceneSelection
  viewport: { width: number; height: number }
}) {
  // Sit beside the click, on whichever side has room.
  const flip =
    viewport.width > 0 && selection.x + POPUP_OFFSET + POPUP_WIDTH > viewport.width
  const left = flip
    ? Math.max(POPUP_MARGIN, selection.x - POPUP_OFFSET - POPUP_WIDTH)
    : selection.x + POPUP_OFFSET

  // Centred on the click, then pulled back inside the viewport vertically. A
  // popup for a structure at the very bottom is otherwise half off-screen.
  const half = POPUP_HEIGHT / 2
  const lowest = Math.max(half + POPUP_MARGIN, viewport.height - half - POPUP_MARGIN)
  const top =
    viewport.height > 0
      ? Math.min(Math.max(selection.y, half + POPUP_MARGIN), lowest)
      : selection.y

  return (
    <div className="pick-popup" style={{ left, top, width: POPUP_WIDTH }}>
      <div className="pick-popup-name">{selection.name}</div>
      <div className="pick-popup-meta">
        {selection.vertices > 0
          ? `${selection.vertices.toLocaleString()} vertices`
          : 'no geometry'}
      </div>
    </div>
  )
}

/**
 * Keeps the popup inside the viewport. Measured rather than guessed: the split
 * between the viewport and the side panel is a grid fraction, so the width the
 * popup has to fit in is not knowable from the click position alone.
 */
function useElementSize(ref: RefObject<HTMLElement | null>) {
  const [size, setSize] = useState({ width: 0, height: 0 })

  useEffect(() => {
    const element = ref.current
    if (!element) return

    const observer = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect
      setSize((current) =>
        current.width === width && current.height === height ? current : { width, height },
      )
    })
    observer.observe(element)
    return () => observer.disconnect()
  }, [ref])

  return size
}

export function Viewport3D({
  source,
  volume,
  results,
  busy,
}: {
  source: SourceSummary | null
  volume: VolumePayload | null
  results: ResultView[]
  busy: boolean
}) {
  const [settings, setSettings] = useState<ViewportSettings>(DEFAULT_SETTINGS)
  const [selection, setSelection] = useState<SceneSelection | null>(null)
  const viewportRef = useRef<HTMLDivElement>(null)
  const viewportSize = useElementSize(viewportRef)

  // Hidden results stay in the model: dropping them here would change the key
  // and force a rebuild, when all that is needed is an actor visibility flip.
  const model = useMemo<SceneModel>(() => {
    const surfaces: SceneSurface[] = []
    for (const result of results) {
      const artifacts = result.job.result?.artifacts ?? []
      // The labels ride beside the mesh, not inside it, so they are matched up
      // by result rather than by artifact.
      const labels = artifacts.find((a) => a.kind === 'labels')
      for (const artifact of artifacts) {
        if (artifact.kind !== 'mesh' || !MESH_FORMATS.has(artifact.format)) continue
        surfaces.push({
          id: result.id,
          meshRef: artifact.ref,
          format: artifact.format,
          visible: result.visible,
          clipped: result.clipped,
          color: result.color,
          labelsRef: labels?.ref ?? null,
          labelsHeaderRef: labels?.header_ref ?? null,
        })
      }
    }

    return {
      volume: volume ? { headerRef: volume.header_ref, binRef: volume.bin_ref } : null,
      surfaces,
    }
  }, [volume, results])

  // A selection outlives the geometry it points at: the result can be deleted,
  // hidden, or replaced by a re-run, and the popup would be left naming a
  // structure that is no longer on screen.
  useEffect(() => {
    setSelection((current) => {
      if (!current) return current
      const surface = model.surfaces.find((s) => s.id === current.surfaceId)
      return surface?.visible ? current : null
    })
  }, [model])

  const latest = [...results].reverse().find((r) => r.job.result)?.job.result ?? null
  const hasContent = model.volume !== null || model.surfaces.length > 0

  return (
    <div className="view-pane" ref={viewportRef}>
      <div className="viewport-overlay">
        <span className="tag">{source?.name ?? 'No source'}</span>
        <span style={{ display: 'flex', gap: 6, alignItems: 'flex-start' }}>
          {busy && <span className="tag">running</span>}
          {latest && <ScaleTag scale={latest.scale} />}
          {hasContent && (
            <ViewportControls
              settings={settings}
              onChange={setSettings}
              valueRange={volume?.header.value_range ?? null}
            />
          )}
        </span>
      </div>

      {hasContent ? (
        <Suspense fallback={<div className="empty-state">Loading renderer…</div>}>
          <Scene
            model={model}
            settings={settings}
            selection={selection}
            onSelect={setSelection}
          />
        </Suspense>
      ) : (
        <div className="empty-state">{placeholderFor(source)}</div>
      )}

      {selection && <PickPopup selection={selection} viewport={viewportSize} />}

      {latest && (
        <div className="viewport-footer">
          <span className="tag mono">{latest.ops.join(' → ')}</span>
          <span className="tag mono">{latest.model_version}</span>
        </div>
      )}
    </div>
  )
}
