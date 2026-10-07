import { Op, type Stage } from '../lib/types'
import { LabelTree } from './LabelTree'

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

/** Past this many structures, extracting all of them is worth a warning. */
const MASS_EXTRACTION_THRESHOLD = 20

/** Matches the backend's default, so the form and a bare API call agree. */
export const DEFAULT_SIMPLIFY_MM = 2.5

export interface PipelineParams {
  threshold: number
  stride: number
  /**
   * Cell size for simplifying the extracted surface, in millimetres. Absolute
   * rather than a multiple of the voxel size, because the two paths run on
   * different grids and a physical size lands both in the same range. 0 keeps
   * every vertex.
   */
  simplify: number
  /**
   * Structures to surface, by name or id.
   *
   * `null` is every structure; `[]` is none picked. The two have to be
   * distinguishable because "select none" is a real state to pass through
   * while picking, and folding it into "all" would make clearing the list
   * silently ask for everything.
   */
  labels: string[] | null
}

/** Every structure, as the backend spells it. */
export const ALL_LABELS = 'all'

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
    // Simplifying applies to both: it is the geometry that costs, either way.
    const shared = { stride: params.stride, simplify: params.simplify }
    return segments
      ? { op, params: { label: params.labels ?? ALL_LABELS, ...shared } }
      : { op, params: { threshold: params.threshold, ...shared } }
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
  running,
  submitting,
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
  /** Jobs submitted and not yet finished. Shown, not gating. */
  running: number
  /** True while a submission is in flight. Gates the button, briefly. */
  submitting: boolean
}) {
  const wantsSurface = selected.includes(Op.ISO_SURFACE)
  const wantsMask = selected.includes(Op.SEGMENT)
  const extracting = params.labels === null ? knownLabels.length : params.labels.length
  // Nothing ticked is a job that would extract nothing. The button says so
  // rather than letting the backend answer with an error.
  const nothingPicked = wantsMask && wantsSurface && params.labels !== null && extracting === 0
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
              <LabelTree
                names={knownLabels}
                selected={params.labels}
                onChange={(labels) => onParamsChange({ ...params, labels })}
              />
              {extracting > MASS_EXTRACTION_THRESHOLD && (
                // Surfacing many structures costs a marching-cubes pass each,
                // and a full `total` segmentation is 117 of them.
                <p className="popover-hint warn-text" style={{ margin: '4px 0 6px' }}>
                  {extracting} structures. That is one surface per structure and can take
                  minutes — narrowing it down is much quicker.
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

          <label className="slider-row" style={{ marginTop: 6 }}>
            <span className="slider-label">simplify</span>
            <input
              type="range"
              min={0}
              max={10}
              step={0.5}
              value={params.simplify}
              onChange={(event) =>
                onParamsChange({ ...params, simplify: Number(event.target.value) })
              }
            />
            <span className="slider-value mono">
              {params.simplify > 0 ? `${params.simplify} mm` : 'off'}
            </span>
          </label>
          <p className="popover-hint" style={{ margin: '2px 0 0' }}>
            {params.simplify > 0
              ? `Collapses detail finer than ${params.simplify} mm. `
              : ''}
          </p>
        </div>
      )}

      {nothingPicked && (
        <p className="popover-hint warn-text" style={{ margin: '4px 0 0' }}>
          No structures picked — pick at least one, or use All.
        </p>
      )}

      <div className="controls">
        <button
          className="primary"
          onClick={onRun}
          disabled={!canRun || selected.length === 0 || nothingPicked || submitting}
        >
          {submitting ? 'Queuing…' : 'Run'}
        </button>
        {/* Reports the queue instead of disabling the button: a long job must
            not stop the next one being submitted behind it. */}
        {running > 0 && (
          <span className="muted" style={{ fontSize: 12 }}>
            {running} running
          </span>
        )}
      </div>
    </div>
  )
}
