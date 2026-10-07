import { Op, type InputShape, type SolverEntry, type Stage } from '../lib/types'
import { Info } from './Info'
import { LabelTree } from './LabelTree'

/**
 * What kind of data this is, and therefore what can be done to it.
 *
 * Workflows rather than source kinds, because one kind of upload can be more
 * than one thing: a single flat radiograph and a set of intraoral photographs
 * both arrive as image files and want completely different pipelines. Which
 * stages are available is a property of the data, so it is chosen explicitly
 * rather than inferred from a file extension.
 */
export interface Workflow {
  id: string
  label: string
  /** In the order the stages must run. `buildStages` sorts the ticks to match. */
  ops: string[]
  hint: string
}

export const WORKFLOWS: Workflow[] = [
  {
    id: 'ct',
    label: 'CT volume',
    // `segment` precedes `iso_surface` because the two chain: stages in one job
    // share an envelope, so a surface extracted after a segmentation follows
    // the mask's label boundaries instead of a single density threshold.
    ops: [Op.ISOLATE_VOLUME, Op.SEGMENT, Op.ISO_SURFACE],
    hint: 'A stack of slices assembled into a volume, then segmented.',
  },
  {
    id: 'photos',
    label: 'Photographs',
    // Poses first, because everything after them is expressed in camera
    // geometry. Deliberately no fixed number of views: a phone that tracks its
    // own motion supplies them, and anything else has them solved.
    ops: [Op.ESTIMATE_POSES, Op.RECTIFY, Op.RECONSTRUCT, Op.SEGMENT, Op.MEASURE],
    hint: 'Any number of photographs from any angles. Poses come from the device tracker when there is one, and are solved from the images otherwise.',
  },
  {
    id: 'panoramic',
    label: 'Panoramic X-ray',
    ops: [Op.PX2TOOTH, Op.SEGMENT, Op.MEASURE],
    hint: 'One flat projection, so the buccolingual extent is inferred rather than observed.',
  },
]

/** What an upload most likely is, before anyone says otherwise. */
export const DEFAULT_WORKFLOW: Record<'dicom' | 'images', string> = {
  dicom: 'ct',
  images: 'photos',
}

export function workflowFor(id: string): Workflow {
  return WORKFLOWS.find((entry) => entry.id === id) ?? WORKFLOWS[0]
}

const OP_HINTS: Record<string, string> = {
  [Op.ISOLATE_VOLUME]: 'Assemble the series into a volume',
  [Op.ISO_SURFACE]: 'Marching cubes at the threshold below — a bone surface',
  [Op.RECTIFY]: 'Undistort and align depth to colour',
  [Op.RECONSTRUCT]: 'Build a surface from the capture',
  [Op.SEGMENT]: 'Find structures — organs in a volume, teeth in a capture',
  [Op.MEASURE]: 'Arch and tooth measurements',
  [Op.ESTIMATE_POSES]:
    'Camera positions for the photographs — reported by the device when it tracks its own motion, solved from the images otherwise',
  [Op.PX2TOOTH]: 'Teeth from a single panoramic radiograph',
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
  /**
   * Which framework solves the camera poses.
   *
   * `null` is "whichever is installed", which is the right default and the only
   * choice available when none are. Naming one is how two frameworks get
   * compared on the same photographs — each export records which ran.
   */
  solver: string | null
}

/** Ticked automatically with the workflow; nothing here forces it. */
export const SOLVER_OP = Op.ESTIMATE_POSES

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
    if (op === Op.ESTIMATE_POSES) {
      // Omitted rather than sent as null when nothing was named: an absent
      // parameter means "whichever is installed", and the backend treats an
      // empty string the same way, so there is one spelling of that intent.
      return params.solver ? { op, params: { solver: params.solver } } : { op }
    }
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

/**
 * Which frameworks the form may offer, given what the capture actually is.
 *
 * The shape is not a preference — it is a property of the data. A folder of
 * stills has no order for a SLAM tracker to follow, and offering ORB-SLAM2 for
 * it would let someone pick a solver that cannot answer. So the list is filtered
 * by shape as well as by what is installed, and the reason is shown rather than
 * the entry being silently absent.
 */
export function solverChoices(
  solvers: SolverEntry[],
  shape: InputShape,
): { entry: SolverEntry; offered: boolean; why: string | null }[] {
  return solvers
    .filter((entry) => entry.name !== 'device')
    .map((entry) => {
      if (!entry.accepts.includes(shape)) {
        return {
          entry,
          offered: false,
          why: `takes ${entry.accepts.map(shapeLabel).join(' or ')}, not ${shapeLabel(shape)}`,
        }
      }
      if (!entry.available) {
        return { entry, offered: false, why: entry.detail ?? 'not available here' }
      }
      return { entry, offered: true, why: null }
    })
}

export function shapeLabel(shape: string): string {
  switch (shape) {
    case 'unordered_images':
      return 'an unordered set'
    case 'image_sequence':
      return 'a sequence'
    case 'device_poses':
      return 'device poses'
    default:
      return shape
  }
}

/** The shape a source presents, as far as the form can tell. */
export function shapeFor(kind: 'dicom' | 'images', hasDepth: boolean): InputShape {
  return kind === 'images' && hasDepth ? 'image_sequence' : 'unordered_images'
}

export function PipelineSection({
  ops,
  workflow,
  onWorkflowChange,
  selected,
  onToggle,
  params,
  onParamsChange,
  valueRange,
  knownLabels,
  solvers,
  shape,
  onRun,
  canRun,
  running,
  submitting,
}: {
  ops: string[]
  workflow: string
  onWorkflowChange: (id: string) => void
  selected: string[]
  onToggle: (op: string) => void
  params: PipelineParams
  onParamsChange: (params: PipelineParams) => void
  valueRange: [number, number] | null
  /** Class names from a previous segmentation of this source, if any. */
  knownLabels: string[]
  /** Camera-pose frameworks this deployment knows about. Empty until fetched. */
  solvers: SolverEntry[]
  /** What this source presents, which decides which solvers are usable. */
  shape: InputShape
  onRun: () => void
  canRun: boolean
  /** Jobs submitted and not yet finished. Shown, not gating. */
  running: number
  /** True while a submission is in flight. Gates the button, briefly. */
  submitting: boolean
}) {
  const wantsSurface = selected.includes(Op.ISO_SURFACE)
  const wantsMask = selected.includes(Op.SEGMENT)
  const wantsPoses = selected.includes(SOLVER_OP)
  const choices = solverChoices(solvers, shape)
  const usable = choices.filter((choice) => choice.offered)
  // A named solver the capture cannot support is worse than no choice at all:
  // the job fails at the backend with the reason, after the upload. Cleared here
  // so the selection cannot outlive the data it was made for.
  const stale = params.solver !== null && !usable.some((c) => c.entry.name === params.solver)
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

      <label className="field" style={{ marginBottom: 6 }}>
        <span className="field-label">workflow</span>
        <select
          value={workflow}
          onChange={(event) => onWorkflowChange(event.target.value)}
          title={workflowFor(workflow).hint}
        >
          {WORKFLOWS.map((entry) => (
            <option key={entry.id} value={entry.id}>
              {entry.label}
            </option>
          ))}
        </select>
      </label>

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
                // A warning, not a note: it changes what the operator should do.
                // Surfacing many structures costs a marching-cubes pass each, and
                // a full `total` segmentation is 117 of them.
                <p className="popover-hint warn-text" style={{ margin: '4px 0 6px' }}>
                  {extracting} structures — one surface each, which can take minutes.
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
                {valueRange ? `HU, ${Math.round(valueRange[0])} … ${Math.round(valueRange[1])}` : 'load a volume'}
                <Info
                  text={
                    valueRange
                      ? `In the volume's own units — Hounsfield for a CT. This series spans ${Math.round(valueRange[0])} to ${Math.round(valueRange[1])}.`
                      : 'Load a volume and this becomes settable in the data’s real units.'
                  }
                />
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
            subsamples the input before extraction
            <Info
              text={
                wantsMask
                  ? 'Subsamples the mask before extraction. Higher is faster and coarser, and every vertex still carries its label. This is coarser than simplifying the finished surface, because a thin structure can vanish between sampled voxels.'
                  : 'Voxel subsampling. Higher is faster and coarser — useful for finding the right threshold before extracting at full resolution.'
              }
            />
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

      {wantsPoses && (
        <div
          className="plane-block"
          style={{ marginTop: 8, paddingTop: 6, borderTop: '1px solid var(--border)' }}
        >
          <label className="field" style={{ marginBottom: 4 }}>
            <span className="field-label">camera poses</span>
            <select
              value={stale ? '' : (params.solver ?? '')}
              onChange={(event) =>
                onParamsChange({ ...params, solver: event.target.value || null })
              }
            >
              <option value="">
                {usable.length > 0 ? 'whichever is installed' : 'none available'}
              </option>
              {usable.map(({ entry }) => (
                <option key={entry.name} value={entry.name}>
                  {entry.tool}
                </option>
              ))}
            </select>
          </label>

          {usable.length === 0 ? (
            <p className="popover-hint warn-text" style={{ margin: '2px 0 0' }}>
              No solver is installed here, so poses will not be solved and the geometry is a
              placeholder.
              <Info
                text={
                  'A capture that carries its own camera positions — a phone that tracked its own ' +
                  'motion — does not need any of these and is never re-solved. These are for ' +
                  'photographs that do not.'
                }
              />
            </p>
          ) : (
            <p className="popover-hint" style={{ margin: '2px 0 0' }}>
              {params.solver
                ? (solvers.find((s) => s.name === params.solver)?.algorithm ?? '')
                : 'Photographs are solved by whichever framework is installed.'}
              <Info
                text={
                  'Which framework ran is recorded on the result, so the same photographs put ' +
                  'through two of them can be compared rather than merely differing.'
                }
              />
            </p>
          )}

          {/* The ones it cannot offer, and why, rather than quietly omitting
              them — an absent option reads as a missing feature, and here the
              reason is usually the data rather than the deployment. */}
          {choices.filter((choice) => !choice.offered).length > 0 && (
            <ul className="solver-unavailable">
              {choices
                .filter((choice) => !choice.offered)
                .map(({ entry, why }) => (
                  <li key={entry.name} className="popover-hint">
                    <span className="mono">{entry.tool}</span> — {why}
                    <span className="solver-licence">{entry.licence}</span>
                  </li>
                ))}
            </ul>
          )}
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
