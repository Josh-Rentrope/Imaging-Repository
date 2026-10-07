import { useState } from 'react'

import { api } from '../lib/api'
import { Info } from './Info'
import type { ResultView } from './ResultsSection'

/**
 * Formats offered, and what each is actually for.
 *
 * FBX is absent on purpose: it is a proprietary binary format needing the
 * Autodesk SDK to write, and an approximation would make files that open in
 * some tools and not others — worse than not offering it.
 */
const FORMATS = [
  { id: 'stl', label: 'STL', note: 'Meshes only, no colour. The safest bet for CAD and printing.' },
  { id: 'obj', label: 'OBJ', note: 'Meshes plus a material file, so colour travels with them.' },
  { id: 'ply', label: 'PLY', note: 'Exactly what the viewer loaded, vertex labels included.' },
]

/** Only a finished result with a mesh has anything to give. */
function exportable(result: ResultView): boolean {
  return (result.job.result?.artifacts ?? []).some(
    (artifact) => artifact.kind === 'mesh' && artifact.format === 'ply',
  )
}

export function ExportSection({ results }: { results: ResultView[] }) {
  const [selected, setSelected] = useState<string[] | null>(null)
  const [format, setFormat] = useState('stl')
  const [labels, setLabels] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const available = results.filter(exportable)
  // Null means "everything exportable", so results arriving later join in.
  const chosen = selected ?? available.map((result) => result.id)
  const hasLabelled = available.some((result) =>
    (result.job.result?.artifacts ?? []).some((artifact) => artifact.kind === 'labels'),
  )

  async function download() {
    setBusy(true)
    setError(null)
    try {
      const archive = await api.exportResults({
        result_ids: chosen,
        format,
        include_labels: labels,
      })
      // The object URL has to be revoked, and the anchor has to be in the
      // document for a click to count as a user gesture in every browser.
      const url = URL.createObjectURL(archive)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = 'bone-viewer-export.zip'
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      URL.revokeObjectURL(url)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
    }
  }

  if (available.length === 0) {
    return (
      <div className="section">
        <h2 className="section-title">Export</h2>
        <p className="muted" style={{ margin: 0 }}>
          Run something that produces geometry.
        </p>
      </div>
    )
  }

  return (
    <div className="section">
      <h2 className="section-title">
        Export
        {available.length > 1 && (
          <span className="section-count">
            <button className="ghost" onClick={() => setSelected(available.map((r) => r.id))}>
              all
            </button>
            <button className="ghost" onClick={() => setSelected([])}>
              none
            </button>
          </span>
        )}
      </h2>

      {/* Layers: one entry per result, because a run that produced several
          meshes is several things to hand over, not one. */}
      <ul className="source-list">
        {available.map((result) => (
          <li key={result.id}>
            <label className="field">
              <input
                type="checkbox"
                checked={chosen.includes(result.id)}
                onChange={(event) =>
                  setSelected(
                    event.target.checked
                      ? [...chosen, result.id]
                      : chosen.filter((id) => id !== result.id),
                  )
                }
              />
              <span className="field-label">{result.label}</span>
            </label>
          </li>
        ))}
      </ul>

      <label className="field" style={{ marginTop: 6 }}>
        <span className="field-label">format</span>
        <select value={format} onChange={(event) => setFormat(event.target.value)}>
          {FORMATS.map((entry) => (
            <option key={entry.id} value={entry.id}>
              {entry.label}
            </option>
          ))}
        </select>
      </label>
      <p className="popover-hint" style={{ margin: '2px 0 6px' }}>
        {FORMATS.find((entry) => entry.id === format)?.note}
      </p>

      {hasLabelled && (
        <label className="field">
          <input
            type="checkbox"
            checked={labels}
            onChange={(event) => setLabels(event.target.checked)}
          />
          <span className="field-label">include vertex labels</span>
          <Info text="One int32 per vertex, beside the mesh it belongs to. The converters preserve vertex order, so the array still lines up. Without it the mesh is just shapes — nothing says which triangle is which tooth." />
        </label>
      )}

      <div className="controls">
        <button className="primary" onClick={download} disabled={busy || chosen.length === 0}>
          {busy ? 'Packaging…' : 'Download .zip'}
        </button>
      </div>

      <p className="popover-hint" style={{ margin: '6px 0 0' }}>
        includes a manifest
        <Info text="Every archive carries a manifest.json describing how the geometry was made: the source, each stage that ran, the parameters it ran with, the tool and algorithm behind it, and whether the scale is real. A mesh without that is a shape of unknown size made by an unknown process." />
      </p>

      {error && (
        <p className="error-text" style={{ margin: '6px 0 0', fontSize: 11 }}>
          {error}
        </p>
      )}
    </div>
  )
}
