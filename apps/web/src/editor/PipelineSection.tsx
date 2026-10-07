import { Op } from '../lib/types'

/** Operations offered per source kind. A volume and an image set need different work. */
export const OPS_BY_KIND: Record<'dicom' | 'images', string[]> = {
  dicom: [Op.ISOLATE_VOLUME],
  images: [Op.RECTIFY, Op.RECONSTRUCT, Op.SEGMENT, Op.MEASURE],
}

export function PipelineSection({
  ops,
  selected,
  onToggle,
  onRun,
  canRun,
  busy,
}: {
  ops: string[]
  selected: string[]
  onToggle: (op: string) => void
  onRun: () => void
  canRun: boolean
  busy: boolean
}) {
  return (
    <div className="section">
      <h2 className="section-title">Pipeline</h2>

      {ops.length === 0 ? (
        <p className="muted" style={{ margin: 0 }}>
          Select a source.
        </p>
      ) : (
        ops.map((op) => (
          <label key={op} className="field">
            <input
              type="checkbox"
              checked={selected.includes(op)}
              onChange={() => onToggle(op)}
            />
            <span className="field-label mono">{op}</span>
          </label>
        ))
      )}

      <div className="controls">
        <button
          className="primary"
          onClick={onRun}
          disabled={!canRun || busy || selected.length === 0}
        >
          {busy ? 'Running…' : 'Run'}
        </button>
      </div>
    </div>
  )
}
