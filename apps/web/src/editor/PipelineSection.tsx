import { Op, type Stage } from '../lib/types'

/** Operations offered per source kind. A volume and an image set need different work. */
export const OPS_BY_KIND: Record<'dicom' | 'images', string[]> = {
  dicom: [Op.ISOLATE_VOLUME, Op.ISO_SURFACE],
  images: [Op.RECTIFY, Op.RECONSTRUCT, Op.SEGMENT, Op.MEASURE],
}

const OP_HINTS: Record<string, string> = {
  [Op.ISOLATE_VOLUME]: 'Assemble the series into a volume',
  [Op.ISO_SURFACE]: 'Marching cubes at the threshold below — a bone surface',
  [Op.RECTIFY]: 'Undistort and align depth to colour',
  [Op.RECONSTRUCT]: 'Build a surface from the capture',
  [Op.SEGMENT]: 'Per-tooth instances with FDI labels',
  [Op.MEASURE]: 'Arch and tooth measurements',
}

export const DEFAULT_THRESHOLD = 300

export interface PipelineParams {
  threshold: number
  stride: number
}

export function buildStages(selected: string[], params: PipelineParams): Stage[] {
  return selected.map((op) =>
    op === Op.ISO_SURFACE
      ? { op, params: { threshold: params.threshold, stride: params.stride } }
      : { op },
  )
}

export function PipelineSection({
  ops,
  selected,
  onToggle,
  params,
  onParamsChange,
  valueRange,
  onRun,
  canRun,
  busy,
}: {
  ops: string[]
  selected: string[]
  onToggle: (op: string) => void
  params: PipelineParams
  onParamsChange: (params: PipelineParams) => void
  valueRange: [number, number] | null
  onRun: () => void
  canRun: boolean
  busy: boolean
}) {
  const wantsSurface = selected.includes(Op.ISO_SURFACE)
  const span = valueRange ? valueRange[1] - valueRange[0] : 0
  // A twentieth of the range is a usable step whatever the units turn out to be.
  const step = valueRange ? Math.max(1, Math.round(span / 200)) : 10

  return (
    <div className="section">
      <h2 className="section-title">Pipeline</h2>

      {ops.length === 0 ? (
        <p className="muted" style={{ margin: 0 }}>
          Select a source.
        </p>
      ) : (
        ops.map((op) => (
          <label key={op} className="field" title={OP_HINTS[op]}>
            <input type="checkbox" checked={selected.includes(op)} onChange={() => onToggle(op)} />
            <span className="field-label mono">{op}</span>
          </label>
        ))
      )}

      {wantsSurface && (
        <div className="plane-block" style={{ marginTop: 8, paddingTop: 6, borderTop: '1px solid var(--border)' }}>
          <label className="slider-row">
            <span className="slider-label">threshold</span>
            <input
              type="range"
              min={valueRange ? valueRange[0] : 0}
              max={valueRange ? valueRange[1] : 4000}
              step={step}
              value={params.threshold}
              disabled={!valueRange}
              onChange={(event) =>
                onParamsChange({ ...params, threshold: Number(event.target.value) })
              }
            />
            <input
              className="number-input mono"
              type="number"
              min={valueRange ? valueRange[0] : undefined}
              max={valueRange ? valueRange[1] : undefined}
              step={step}
              value={params.threshold}
              onChange={(event) =>
                onParamsChange({ ...params, threshold: Number(event.target.value) })
              }
            />
          </label>
          <p className="popover-hint" style={{ margin: '2px 0 6px' }}>
            {valueRange
              ? `In the volume's units — Hounsfield for a CT. Range ${Math.round(valueRange[0])} … ${Math.round(valueRange[1])}.`
              : 'Load a volume to set this in real units.'}
          </p>

          <label className="slider-row">
            <span className="slider-label">stride</span>
            <input
              type="range"
              min={1}
              max={6}
              step={1}
              value={params.stride}
              onChange={(event) => onParamsChange({ ...params, stride: Number(event.target.value) })}
            />
            <span className="slider-value mono">{params.stride}×</span>
          </label>
          <p className="popover-hint" style={{ margin: '2px 0 0' }}>
            Voxel subsampling. Higher is faster and coarser — useful for finding the right
            threshold before extracting at full resolution.
          </p>
        </div>
      )}

      <div className="controls">
        <button className="primary" onClick={onRun} disabled={!canRun || busy || selected.length === 0}>
          {busy ? 'Running…' : 'Run'}
        </button>
      </div>
    </div>
  )
}
