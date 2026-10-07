import { lazy, Suspense, useMemo, useState } from 'react'

import type { ResultEnvelope, SourceSummary, VolumePayload } from '../lib/types'
import type { ResultView } from './ResultsSection'
import type { SceneModel, SceneSurface } from './Scene'
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

  // Hidden results stay in the model: dropping them here would change the key
  // and force a rebuild, when all that is needed is an actor visibility flip.
  const model = useMemo<SceneModel>(() => {
    const surfaces: SceneSurface[] = []
    for (const result of results) {
      const artifacts = result.job.result?.artifacts ?? []
      for (const artifact of artifacts) {
        if (artifact.kind !== 'mesh' || !MESH_FORMATS.has(artifact.format)) continue
        surfaces.push({
          id: result.id,
          meshRef: artifact.ref,
          format: artifact.format,
          visible: result.visible,
          clipped: result.clipped,
          color: result.color,
        })
      }
    }

    return {
      volume: volume ? { headerRef: volume.header_ref, binRef: volume.bin_ref } : null,
      surfaces,
    }
  }, [volume, results])

  const latest = [...results].reverse().find((r) => r.job.result)?.job.result ?? null
  const hasContent = model.volume !== null || model.surfaces.length > 0

  return (
    <section className="viewport">
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
          <Scene model={model} settings={settings} />
        </Suspense>
      ) : (
        <div className="empty-state">{placeholderFor(source)}</div>
      )}

      {latest && (
        <div className="viewport-footer">
          <span className="tag mono">{latest.ops.join(' → ')}</span>
          <span className="tag mono">{latest.model_version}</span>
        </div>
      )}
    </section>
  )
}
