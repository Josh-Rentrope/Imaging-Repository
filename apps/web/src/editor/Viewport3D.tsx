import { lazy, Suspense, useEffect, useMemo, useState } from 'react'

import { api } from '../lib/api'
import type { Job, ResultEnvelope, SourceSummary, VolumePayload } from '../lib/types'
import type { SceneInput } from './Scene'
import { ViewportControls } from './ViewportControls'
import { DEFAULT_SETTINGS, type ViewportSettings } from './viewportSettings'

// vtk.js is heavy and is only needed once something is actually being rendered,
// so the whole renderer is behind a dynamic import.
const Scene = lazy(() => import('./Scene').then((module) => ({ default: module.Scene })))

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

function placeholderFor(source: SourceSummary | null, loadingVolume: boolean): string {
  if (!source) return 'Drop a source to begin'
  if (loadingVolume) return 'Loading volume…'
  if (!source.renderable) return source.render_reason ?? 'Nothing to display'
  return 'Select the source'
}

export function Viewport3D({
  source,
  result,
  job,
  busy,
}: {
  source: SourceSummary | null
  result: ResultEnvelope | null
  job: Job | null
  busy: boolean
}) {
  const [volume, setVolume] = useState<VolumePayload | null>(null)
  const [loadingVolume, setLoadingVolume] = useState(false)
  const [settings, setSettings] = useState<ViewportSettings>(DEFAULT_SETTINGS)

  const renderableDicom = source?.kind === 'dicom' && source.renderable

  useEffect(() => {
    if (!source || !renderableDicom) {
      setVolume(null)
      return
    }

    let cancelled = false
    setLoadingVolume(true)
    api
      .getVolume(source.source_id)
      .then((payload) => {
        if (!cancelled) setVolume(payload)
      })
      .catch(() => {
        if (!cancelled) setVolume(null)
      })
      .finally(() => {
        if (!cancelled) setLoadingVolume(false)
      })

    return () => {
      cancelled = true
    }
  }, [source, renderableDicom])

  // A pipeline result takes precedence over the source's own volume, so running
  // segmentation on a series shows the derived geometry rather than the raw scan.
  const mesh = result?.artifacts.find((a) => a.kind === 'mesh') ?? null
  const input = useMemo<SceneInput | null>(() => {
    if (mesh) return { kind: 'surface', meshRef: mesh.ref, format: mesh.format }
    if (volume) return { kind: 'volume', headerRef: volume.header_ref, binRef: volume.bin_ref }
    return null
  }, [mesh?.ref, mesh?.format, volume])

  const hasVolume = input?.kind === 'volume'

  return (
    <section className="viewport">
      <div className="viewport-overlay">
        <span className="tag">{source?.name ?? 'No source'}</span>
        <span style={{ display: 'flex', gap: 6, alignItems: 'flex-start' }}>
          {busy && <span className="tag">running</span>}
          {result && <ScaleTag scale={result.scale} />}
          {input && (
            <ViewportControls
              settings={settings}
              onChange={setSettings}
              valueRange={hasVolume ? (volume?.header.value_range ?? null) : null}
            />
          )}
        </span>
      </div>

      {input ? (
        <Suspense fallback={<div className="empty-state">Loading renderer…</div>}>
          <Scene input={input} settings={settings} />
        </Suspense>
      ) : (
        <div className="empty-state">{placeholderFor(source, loadingVolume)}</div>
      )}

      {result && (
        <div className="viewport-footer">
          <span className="tag mono">{result.ops.join(' → ')}</span>
          <span className="tag mono">{result.model_version}</span>
          {job?.backend && <span className="tag">{job.backend}</span>}
        </div>
      )}
    </section>
  )
}
