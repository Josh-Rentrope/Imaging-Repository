import type { Job, ResultEnvelope } from '../lib/types'
import type { Source } from '../state/editor'
import { Scene } from './Scene'

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

function placeholderFor(source: Source | null): string {
  if (!source) return 'Drop a source to begin'
  if (source.status === 'busy') return 'Processing…'
  if (source.kind === 'dicom') return 'Volume rendering not implemented'
  return 'No geometry yet — run the pipeline'
}

export function Viewport3D({
  source,
  result,
  job,
  busy,
}: {
  source: Source | null
  result: ResultEnvelope | null
  job: Job | null
  busy: boolean
}) {
  const mesh = result?.artifacts.find((a) => a.kind === 'mesh') ?? null

  return (
    <section className="viewport">
      <div className="viewport-overlay">
        <span className="tag">{source?.name ?? 'No source'}</span>
        <span style={{ display: 'flex', gap: 6 }}>
          {busy && <span className="tag">running</span>}
          {result && <ScaleTag scale={result.scale} />}
        </span>
      </div>

      {mesh ? (
        <Scene meshRef={mesh.ref} units={mesh.units ?? 'arbitrary'} />
      ) : (
        <div className="empty-state">
          <span>{placeholderFor(source)}</span>
        </div>
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
