import { Op, type InputShape, type SolverEntry, type Stage } from '../lib/types'
import { Info } from './Info'
import { LabelTree } from './LabelTree'
import { Section } from './Section'

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

/** Enough of a source to guess its workflow from. */
export interface WorkflowHint {
  kind: 'dicom' | 'images'
  modality: string | null
  renderable: boolean
}

/**
 * The workflow an upload most likely wants.
 *
 * `DEFAULT_WORKFLOW` maps by source kind, which is right for photographs and
 * wrong for a good deal of DICOM: a chest X-ray and a mammogram are both single
 * projections, not volumes, and handing them the CT workflow offers to assemble
 * a series that has one slice in it. So the modality decides where it can, and
 * volume-ness is taken from whether a volume was actually built rather than
 * assumed from the kind.
 *
 * Nothing here is forced — the workflow selector is right there — it only picks
 * the starting point.
 */
export function suggestWorkflow(source: WorkflowHint): string {
  if (source.kind === 'images') return 'photos'

  // A built volume is the one unambiguous signal that this is a stack.
  if (source.renderable) return 'ct'

  // Single-frame projections with a dedicated pipeline. Panoramic is the only
  // one of those the app has, and it is genuinely modality-specific, so it is
  // claimed only for the modality it belongs to.
  if (source.modality === 'PX') return 'panoramic'

  // Everything else — DX, MG, US, NM, PT, and DICOM that would not assemble —
  // falls back to its kind's default and is left for the operator to correct.
  return DEFAULT_WORKFLOW[source.kind]
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
  /**
   * How the named solver is configured. Only ever sent when COLMAP runs — see
   * `colmapInPlay`. Held here rather than in the form so the values survive
   * changing workflow, which is what makes two runs on the same photographs
   * comparable.
   */
  solverOptions: SolverOptions
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
  colmap: boolean,
): Stage[] {
  const segments = selected.includes(Op.SEGMENT)
  const sequence = [...selected].sort((a, b) => ordered.indexOf(a) - ordered.indexOf(b))

  // The solver's name, omitted rather than sent as null when nothing was named:
  // an absent parameter means "whichever is installed", and the backend treats
  // an empty string the same way, so there is one spelling of that intent.
  const solving = {
    ...(params.solver ? { solver: params.solver } : {}),
    ...(colmap ? solverParams(params.solverOptions) : {}),
  }
  const carriesSolver = Object.keys(solving).length > 0

  return sequence.map((op) => {
    // Attached to every stage that can start a solve, not only the first. The
    // backend memoises one reconstruction per request, so whichever stage runs
    // first fixes the options for the whole job — and a later stage carrying
    // none would be read as "COLMAP's defaults" rather than as agreement.
    if (op === Op.ESTIMATE_POSES || op === Op.RECONSTRUCT) {
      return carriesSolver ? { op, params: solving } : { op }
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

/** The framework whose options this form knows how to set. */
export const COLMAP = 'colmap'

/**
 * COLMAP's own knobs.
 *
 * Every one is a COLMAP option, which is why they are offered — and sent — only
 * when COLMAP is what will run. Handing them to another framework would be
 * sending parameters that mean nothing to it.
 *
 * The defaults are COLMAP's, with two exceptions. `minTrack` is applied here
 * after the solve rather than by COLMAP, and defaults to 5 per the range a
 * tracker would use. `sequential` is left off and means "as the capture
 * dictates", so ticking it forces sequence matching rather than being the only
 * way to get it.
 */
export interface SolverOptions {
  /** Photographs a point must be seen in before it is shown. */
  minTrack: number
  /** Force matching only between near neighbours. */
  sequential: boolean
  /** Features kept per photograph, before matching. */
  maxNumFeatures: number
  /**
   * SIFT on oriented ellipses instead of circles. Several times the cost, and
   * the lever that matters when a surface looks different from every angle —
   * which is what a specular highlight does.
   */
  estimateAffineShape: boolean
  domainSizePooling: boolean
  /** Lower accepts weaker features, and more of them. */
  peakThreshold: number
  /** Lowe ratio. Lower is stricter about what counts as a match. */
  maxRatio: number
  /** Features two photographs must share to be treated as a pair. */
  minNumMatches: number
  /** Longest side matched at. 0 or less uses the photograph's own size. */
  maxImageSize: number
}

export const DEFAULT_SOLVER_OPTIONS: SolverOptions = {
  minTrack: 5,
  sequential: false,
  maxNumFeatures: 8192,
  estimateAffineShape: false,
  domainSizePooling: false,
  peakThreshold: 0.0067,
  maxRatio: 0.8,
  minNumMatches: 15,
  maxImageSize: 0,
}

/**
 * The wire spelling, in one place so the form and the request cannot drift.
 *
 * `sequential` is sent only when ticked: absent is what asks the backend to
 * decide from the capture, and sending `false` would overrule it.
 */
function solverParams(options: SolverOptions): Record<string, unknown> {
  return {
    min_track: options.minTrack,
    max_num_features: options.maxNumFeatures,
    estimate_affine_shape: options.estimateAffineShape,
    domain_size_pooling: options.domainSizePooling,
    peak_threshold: options.peakThreshold,
    max_ratio: options.maxRatio,
    min_num_matches: options.minNumMatches,
    ...(options.sequential ? { sequential: true } : {}),
    ...(options.maxImageSize > 0 ? { max_image_size: options.maxImageSize } : {}),
  }
}

/**
 * Whether COLMAP is what will actually run.
 *
 * The form and the request have to agree on this or a slider would move without
 * changing anything: the options are COLMAP's, so they are shown when COLMAP is
 * chosen and also when nothing is chosen and COLMAP is the only thing installed,
 * which is the same situation.
 */
export function colmapInPlay(
  solvers: SolverEntry[],
  shape: InputShape,
  chosen: string | null,
): boolean {
  const usable = solverChoices(solvers, shape).filter((choice) => choice.offered)
  if (!usable.some(({ entry }) => entry.name === COLMAP)) return false
  return chosen === COLMAP || (chosen === null && usable.length === 1)
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

  const options = params.solverOptions
  const setOptions = (patch: Partial<SolverOptions>) =>
    onParamsChange({ ...params, solverOptions: { ...options, ...patch } })
  const extracting = params.labels === null ? knownLabels.length : params.labels.length
  // Nothing ticked is a job that would extract nothing. The button says so
  // rather than letting the backend answer with an error.
  const nothingPicked = wantsMask && wantsSurface && params.labels !== null && extracting === 0
  const span = valueRange ? valueRange[1] - valueRange[0] : 0
  // A twentieth of the range is a usable step whatever the units turn out to be.
  const step = valueRange ? Math.max(1, Math.round(span / 200)) : 10

  return (
    <Section id="pipeline" title="Pipeline">
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

          {colmapInPlay(solvers, shape, params.solver) && (
            <div className="solver-rows">
              <p className="popover-hint" style={{ margin: '6px 0 2px' }}>
                colmap
                <Info
                  text={
                    'How COLMAP is asked to work. These are its own options, so they are shown ' +
                    'only when COLMAP is what runs — another framework would be handed names it ' +
                    'has never heard of.\n\n' +
                    'Every one of them is recorded on the result, so two runs over the same ' +
                    'photographs can be told apart afterwards.'
                  }
                />
              </p>

              <label
                className="slider-row solver-row"
                title="A point is shown only if this many photographs saw it. Raising it drops the points a couple of views agreed on, which sit in the air around a subject rather than on it."
              >
                <span className="slider-label">min frames</span>
                <input
                  type="range"
                  min={3}
                  max={20}
                  step={1}
                  value={options.minTrack}
                  onChange={(event) => setOptions({ minTrack: Number(event.target.value) })}
                />
                <span className="slider-value mono">{options.minTrack}</span>
              </label>

              <label
                className="slider-row solver-row"
                title="Features kept per photograph before matching. More finds detail on a plain surface, and takes proportionally longer."
              >
                <span className="slider-label">features</span>
                <select
                  value={options.maxNumFeatures}
                  onChange={(event) => setOptions({ maxNumFeatures: Number(event.target.value) })}
                >
                  <option value={4096}>4k</option>
                  <option value={8192}>8k</option>
                  <option value={16384}>16k</option>
                  <option value={32768}>32k</option>
                </select>
                <span />
              </label>

              <label
                className="slider-row solver-row"
                title="Feature strength a point must reach to be kept. Lower accepts weaker ones: more features everywhere, including on noise."
              >
                <span className="slider-label">weakest</span>
                <input
                  type="range"
                  min={0.002}
                  max={0.012}
                  step={0.0001}
                  value={options.peakThreshold}
                  onChange={(event) =>
                    setOptions({ peakThreshold: Number(event.target.value) })
                  }
                />
                <span className="slider-value mono">{options.peakThreshold.toFixed(4)}</span>
              </label>

              <label
                className="slider-row solver-row"
                title="How much closer a second-best match has to be before a match is accepted — COLMAP's ratio test. Lower is stricter and keeps fewer, better matches."
              >
                <span className="slider-label">strictness</span>
                <input
                  type="range"
                  min={0.6}
                  max={0.95}
                  step={0.01}
                  value={options.maxRatio}
                  onChange={(event) => setOptions({ maxRatio: Number(event.target.value) })}
                />
                <span className="slider-value mono">{options.maxRatio.toFixed(2)}</span>
              </label>

              <label
                className="slider-row solver-row"
                title="Features two photographs must share before they are treated as a pair. Lower links weakly-overlapping views, at the cost of more wrong links."
              >
                <span className="slider-label">pair min</span>
                <input
                  type="range"
                  min={4}
                  max={40}
                  step={1}
                  value={options.minNumMatches}
                  onChange={(event) => setOptions({ minNumMatches: Number(event.target.value) })}
                />
                <span className="slider-value mono">{options.minNumMatches}</span>
              </label>

              <label
                className="slider-row solver-row"
                title="Longest side features are taken at. Lower is much faster and needs far less memory, at the cost of detail — the option to reach for when a capture takes too long."
              >
                <span className="slider-label">max size</span>
                <select
                  value={options.maxImageSize}
                  onChange={(event) => setOptions({ maxImageSize: Number(event.target.value) })}
                >
                  <option value={0}>full</option>
                  <option value={3200}>3200</option>
                  <option value={2400}>2400</option>
                  <option value={1600}>1600</option>
                </select>
                <span />
              </label>

              <label
                className="solver-toggle"
                title="Match each feature to an oriented ellipse rather than a circle, so a patch is still recognised when the surface is seen at a different angle. Several times slower, and the option that matters most for a glossy subject."
              >
                <input
                  type="checkbox"
                  checked={options.estimateAffineShape}
                  onChange={(event) => setOptions({ estimateAffineShape: event.target.checked })}
                />
                <span>shape-adaptive features</span>
              </label>

              <label
                className="solver-toggle"
                title="Pairs with shape-adaptive features: weighs matches so patches at different scales still correspond."
              >
                <input
                  type="checkbox"
                  checked={options.domainSizePooling}
                  onChange={(event) => setOptions({ domainSizePooling: event.target.checked })}
                />
                <span>scale-invariant matching</span>
              </label>

              <label
                className="solver-toggle"
                title="Match only photographs near each other in the order given. Right for frames lifted from a video, wrong for a set of stills, where it would skip the pairs that hold the model together."
              >
                <input
                  type="checkbox"
                  checked={options.sequential}
                  onChange={(event) => setOptions({ sequential: event.target.checked })}
                />
                <span>force sequence matching</span>
              </label>
            </div>
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
    </Section>
  )
}
