import { useState } from 'react'

import type { Job, SourceKind } from '../lib/types'
import { Info } from './Info'
import { ProvenanceHover } from './ProvenancePopup'
import { Section } from './Section'

/** How far the segmentation's box sits from the volume's, said in one line. */
function formatOffset(offset: number[]): string {
  const distance = Math.sqrt(offset.reduce((total, value) => total + value * value, 0))
  return `${Math.round(distance)} mm`
}

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

/** Millions get abbreviated; a raw seven-digit count is unreadable in a row. */
function formatCount(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`
  if (value >= 1_000) return `${Math.round(value / 1_000)}k`
  return value.toLocaleString()
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
  // What simplifying removed, stated rather than assumed — the number is the
  // whole point of turning it on.
  const before = result.geometry?.triangles_before
  if (mesh?.triangles && before && before > mesh.triangles) {
    bits[0] = `${mesh.triangles.toLocaleString()} tris (from ${formatCount(before)})`
  }
  if (result.measurements.length > 0) bits.push(`${result.measurements.length} measures`)
  return bits.length > 0 ? bits.join(' · ') : null
}

function ResultRow({
  result,
  sourceKind,
  workflow,
  onToggleVisible,
  onToggleClipped,
  onRemove,
  onRename,
}: {
  result: ResultView
  sourceKind: SourceKind | null
  workflow: string | null
  onToggleVisible: () => void
  onToggleClipped: () => void
  onRemove: () => void
  onRename: (name: string | null) => void
}) {
  const failed = result.job.status === 'failed' || result.job.status === 'rejected'
  const pending = result.job.status === 'queued' || result.job.status === 'running'
  const note = summary(result.job)
  const warnings = result.job.result?.warnings ?? []
  const placement = result.job.result?.geometry?.mask_placement ?? null
  const [editing, setEditing] = useState<string | null>(null)

  const commitRename = () => {
    if (editing === null) return
    const trimmed = editing.trim()
    // An empty box means "go back to the derived name" rather than an empty row.
    onRename(trimmed === result.label ? null : trimmed || null)
    setEditing(null)
  }

  return (
    <li className="result-item">
      <div className="result-row">
        <span className="axis-dot" style={{ background: rgb(result.color) }} />

        {editing === null ? (
          <ProvenanceHover job={result.job} sourceKind={sourceKind} workflow={workflow}>
            <button
              className="source-item-name result-name"
              title={`${result.detail} — click to rename`}
              onClick={() => setEditing(result.label)}
            >
              {result.label}
            </button>
          </ProvenanceHover>
        ) : (
          <input
            className="text-input"
            autoFocus
            value={editing}
            onChange={(event) => setEditing(event.target.value)}
            onBlur={commitRename}
            onKeyDown={(event) => {
              if (event.key === 'Enter') commitRename()
              if (event.key === 'Escape') setEditing(null)
            }}
          />
        )}

        {!failed && !pending && (
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
      ) : pending ? (
        <div className="result-meta mono pending-text">{result.job.status}…</div>
      ) : (
        <>
          <div className="result-meta mono">
            {/* A warning nobody hovers over is a warning nobody reads, so the
                marker is on the row and the text is in the popup. */}
            {warnings.length > 0 && (
              <span className="result-warn" title={warnings.join('\n\n')}>
                ⚠{warnings.length > 1 ? ` ${warnings.length}` : ''}{' '}
              </span>
            )}
            {note ?? result.detail}
          </div>
          {/* Plus the placement check, when it ran. This is the one number that
              says whether the segmentation landed on the scan it came from, and
              it is unreadable from the geometry itself. */}
          {placement && (
            <div className={`result-meta mono${placement.agrees ? '' : ' warn-text'}`}>
              mask {placement.overlap_pct.toFixed(1)}% inside volume
              {placement.agrees ? '' : ` · off by ${formatOffset(placement.centre_offset_mm)}`}
            </div>
          )}
        </>
      )}
    </li>
  )
}

export function ResultsSection({
  results,
  sourceKind,
  workflow,
  onToggleVisible,
  onToggleClipped,
  onRemove,
  onRename,
  onClear,
}: {
  results: ResultView[]
  /** Shared by every row: results are listed per source. */
  sourceKind: SourceKind | null
  workflow: string | null
  onToggleVisible: (id: string) => void
  onToggleClipped: (id: string) => void
  onRemove: (id: string) => void
  onRename: (id: string, name: string | null) => void
  onClear: () => void
}) {
  if (results.length === 0) return null

  const shown = results.filter((r) => r.visible).length

  return (
    <Section
      id="results"
      title="Results"
      count={`${shown}/${results.length}`}
      // `clear` deletes every result, so it must not also collapse the panel it
      // lives in — the count is a sibling of the toggle for the same reason.
      actions={
        <button onClick={onClear} title="Delete every result">
          clear
        </button>
      }
    >
      <ul className="source-list">
        {results.map((result) => (
          <ResultRow
            key={result.id}
            result={result}
            sourceKind={sourceKind}
            workflow={workflow}
            onToggleVisible={() => onToggleVisible(result.id)}
            onToggleClipped={() => onToggleClipped(result.id)}
            onRemove={() => onRemove(result.id)}
            onRename={(name) => onRename(result.id, name)}
          />
        ))}
      </ul>

      <p className="popover-hint" style={{ margin: '8px 0 0' }}>
        each result has its own settings
        <Info text="Change the parameters above and run again to compare — a new result is added rather than replacing the last one, and each keeps its own visibility, clipping and parameters. ✂ marks the ones the cutting planes act on." />
      </p>
    </Section>
  )
}
