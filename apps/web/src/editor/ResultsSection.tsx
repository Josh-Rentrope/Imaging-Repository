import type { Job } from '../lib/types'

/** A pipeline result being displayed, with its own display flags. */
export interface ResultView {
  id: string
  job: Job
  label: string
  detail: string
  visible: boolean
  /** Whether the cutting planes apply to this result's geometry. */
  clipped: boolean
  color: [number, number, number]
}

function rgb(color: [number, number, number]): string {
  return `rgb(${color.map((c) => Math.round(c * 255)).join(',')})`
}

function summary(job: Job): string | null {
  const result = job.result
  if (!result) return null

  const bits: string[] = []
  const mesh = result.artifacts.find((a) => a.kind === 'mesh')
  if (mesh?.triangles) bits.push(`${mesh.triangles.toLocaleString()} tris`)
  if (result.geometry?.threshold != null) bits.push(`${Math.round(result.geometry.threshold)} HU`)
  // The tell for a surface that can be clicked: one extracted from a mask
  // carries labels, one from a density threshold carries none.
  const labels = result.geometry?.labels
  if (labels && labels.length > 0) bits.push(`${labels.length} labels`)
  if (result.measurements.length > 0) bits.push(`${result.measurements.length} measures`)
  return bits.length > 0 ? bits.join(' · ') : null
}

function ResultRow({
  result,
  onToggleVisible,
  onToggleClipped,
  onRemove,
}: {
  result: ResultView
  onToggleVisible: () => void
  onToggleClipped: () => void
  onRemove: () => void
}) {
  const failed = result.job.status === 'failed' || result.job.status === 'rejected'
  const note = summary(result.job)

  return (
    <li className="result-item">
      <div className="result-row">
        <span className="axis-dot" style={{ background: rgb(result.color) }} />
        <span className="source-item-name" title={result.detail}>
          {result.label}
        </span>

        {!failed && (
          <>
            <button
              className="ghost"
              title={result.visible ? 'Hide in the viewport' : 'Show in the viewport'}
              onClick={onToggleVisible}
            >
              {result.visible ? '👁' : '🚫'}
            </button>
            <button
              className="ghost"
              title={result.clipped ? 'Exempt from the cutting planes' : 'Subject to the cutting planes'}
              onClick={onToggleClipped}
            >
              {result.clipped ? '✂' : '—'}
            </button>
          </>
        )}
        <button className="ghost" title="Delete this result" onClick={onRemove}>
          ✕
        </button>
      </div>

      {failed ? (
        <p className="error-text" style={{ margin: '2px 0 0', fontSize: 11 }}>
          {result.job.error}
        </p>
      ) : (
        <div className="result-meta mono">{note ?? result.detail}</div>
      )}
    </li>
  )
}

export function ResultsSection({
  results,
  onToggleVisible,
  onToggleClipped,
  onRemove,
  onClear,
}: {
  results: ResultView[]
  onToggleVisible: (id: string) => void
  onToggleClipped: (id: string) => void
  onRemove: (id: string) => void
  onClear: () => void
}) {
  if (results.length === 0) return null

  const shown = results.filter((r) => r.visible).length

  return (
    <div className="section">
      <h2 className="section-title">
        Results
        <span className="section-count">
          {shown}/{results.length}
          <button onClick={onClear} title="Delete every result" style={{ marginLeft: 8, padding: '0 6px' }}>
            clear
          </button>
        </span>
      </h2>

      <ul className="source-list">
        {results.map((result) => (
          <ResultRow
            key={result.id}
            result={result}
            onToggleVisible={() => onToggleVisible(result.id)}
            onToggleClipped={() => onToggleClipped(result.id)}
            onRemove={() => onRemove(result.id)}
          />
        ))}
      </ul>

      <p className="popover-hint" style={{ margin: '8px 0 0' }}>
        <span className="mono">✂</span> marks a result the cutting planes act on. Change the
        parameters above and run again to compare — each result keeps its own settings.
      </p>
    </div>
  )
}
