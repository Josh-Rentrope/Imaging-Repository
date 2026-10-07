import { Op, type Stage } from '../lib/types'

/** Operations offered per source kind. A volume and an image set need different work. */
export const OPS_BY_KIND: Record<'dicom' | 'images', string[]> = {
  // `segment` comes before `iso_surface` because the two chain: stages in one
  // job share an envelope, and a surface extracted after a segmentation follows
  // the mask's label boundaries instead of a single density threshold.
  dicom: [Op.ISOLATE_VOLUME, Op.SEGMENT, Op.ISO_SURFACE],
  images: [Op.RECTIFY, Op.RECONSTRUCT, Op.SEGMENT, Op.MEASURE],
}

const OP_HINTS: Record<string, string> = {
  [Op.ISOLATE_VOLUME]: 'Assemble the series into a volume',
  [Op.ISO_SURFACE]: 'Marching cubes at the threshold below — a bone surface',
  [Op.RECTIFY]: 'Undistort and align depth to colour',
  [Op.RECONSTRUCT]: 'Build a surface from the capture',
  [Op.SEGMENT]: 'Find structures — organs in a volume, teeth in a capture',
  [Op.MEASURE]: 'Arch and tooth measurements',
}

export const DEFAULT_THRESHOLD = 300

/** Every label, as a surface that can be clicked. */
export const ALL_LABELS = 'all'

/** Past this many structures, extracting all of them is worth a warning. */
const MASS_EXTRACTION_THRESHOLD = 20

export interface PipelineParams {
  threshold: number
  stride: number
  /** Which structure to surface. Only the masked path can honour it. */
  label: string
}

/** The op that turns a segmentation into something visible. */
export const SURFACE_AFTER_MASK = Op.ISO_SURFACE

/**
 * Stages in the order the kind declares, not the order they were ticked.
 *
 * The pipeline is a sequence and the sequence is set by data dependency:
 * `iso_surface` reads the mask `segment` produced, so it has to follow it. Ticks
 * arrive in whatever order the user clicked them, which would otherwise send
 * `iso_surface` first — where it finds no mask, silently falls back to a density
 * threshold, and needs a parameter the form is not even showing.
 */
export function buildStages(
  ordered: string[],
  selected: string[],
  params: PipelineParams,
): Stage[] {
  const segments = selected.includes(Op.SEGMENT)
  const sequence = [...selected].sort((a, b) => ordered.indexOf(a) - ordered.indexOf(b))

  return sequence.map((op) => {
    if (op !== Op.ISO_SURFACE) return { op }
    // The threshold is meaningless once a mask is driving the surface, and a
    // label is meaningless without one, so neither is sent in the wrong case.
    return segments
      ? { op, params: { label: params.label, stride: params.stride } }
      : { op, params: { threshold: params.threshold, stride: params.stride } }
  })
}

export function PipelineSection({
  ops,
  selected,
  onToggle,
  params,
  onParamsChange,
  valueRange,
  knownLabels,
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
  /** Class names from a previous segmentation of this source, if any. */
  knownLabels: string[]
  onRun: () => void
  canRun: boolean
  busy: boolean
}) {
  const wantsSurface = selected.includes(Op.ISO_SURFACE)
  const wantsMask = selected.includes(Op.SEGMENT)
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
          {wantsMask && (
            <>
              <label className="slider-row">
                <span className="slider-label">label</span>
                <input
                  className="text-input mono"
                  type="text"
                  list="known-labels"
                  value={params.label}
                  onChange={(event) => onParamsChange({ ...params, label: event.target.value })}
                />
              </label>
              {/* Names come from a previous run's mask, so the first run of a
                  pair is typed and later ones are picked. */}
              <datalist id="known-labels">
                <option value={ALL_LABELS} />
                {knownLabels.map((name) => (
                  <option key={name} value={name} />
                ))}
              </datalist>
              <p className="popover-hint" style={{ margin: '2px 0 6px' }}>
                {knownLabels.length > 0
                  ? `A structure name or number, or “${ALL_LABELS}”. Known: ${knownLabels.slice(0, 6).join(', ')}${knownLabels.length > 6 ? '…' : ''}.`
                  : `A structure name or number, or “${ALL_LABELS}”. Names appear here after a segmentation has run.`}
              </p>
              {params.label === ALL_LABELS && knownLabels.length > MASS_EXTRACTION_THRESHOLD && (
                // Surfacing every structure costs a surface per structure, and
                // a full `total` segmentation is 117 of them.
                <p className="popover-hint warn-text" style={{ margin: '0 0 6px' }}>
                  All {knownLabels.length} structures. That is one surface per structure and can
                  take minutes — naming one is much quicker.
                </p>
              )}
            </>
          )}

          {!wantsMask && (
            <>
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
            </>
          )}

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
            {wantsMask
              ? 'Subsamples the mask before extraction. Higher is faster and coarser, and every vertex still carries its label.'
              : 'Voxel subsampling. Higher is faster and coarser — useful for finding the right threshold before extracting at full resolution.'}
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
