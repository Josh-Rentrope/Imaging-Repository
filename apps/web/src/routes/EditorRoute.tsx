import { useEffect, useMemo, useRef, useState } from 'react'

import {
  ALL_LABELS,
  buildStages,
  colmapInPlay,
  DEFAULT_SIMPLIFY_MM,
  DEFAULT_SOLVER_OPTIONS,
  DEFAULT_THRESHOLD,
  DEFAULT_WORKFLOW,
  PipelineSection,
  shapeFor,
  solverChoices,
  suggestWorkflow,
  SURFACE_AFTER_MASK,
  workflowFor,
  type PipelineParams,
} from '../editor/PipelineSection'
import { randomId } from '../lib/ids'
import { isLive, settled, subscribeProgress, watch, type Progress } from '../lib/stream'
import { ResultsSection, type ResultView } from '../editor/ResultsSection'
import { SourceSection } from '../editor/SourceSection'
import { ExportSection } from '../editor/ExportSection'
import { MainPanel } from '../editor/MainPanel'
import { surfaceColor } from '../editor/viewportSettings'
import { api } from '../lib/api'
import {
  Op,
  type Job,
  type SegmenterClasses,
  type SolverEntry,
  type SourceSummary,
  type VolumePayload,
} from '../lib/types'
import { useEditor } from '../state/editor'

//: A segmentation on a full CT runs for minutes, so the wait is budgeted in tens
//: of minutes rather than the seconds a marching-cubes pass takes.
const POLL_INTERVAL_MS = 1500
const POLL_ATTEMPTS = 800

/**
 * How often to poll while the event stream is up.
 *
 * Polling is not removed, only demoted: the stream carries the updates, and this
 * is what keeps a socket that will not open from being a broken app. Slow enough
 * that it costs nothing while the stream is doing the work, and it speeds back
 * up by itself the moment the socket drops.
 */
const POLL_FALLBACK_MS = 15_000

/** How long Run stays disabled after a click, whatever the server does. */
const SUBMIT_LOCK_MS = 1000

/**
 * How long Run waits for the server to acknowledge before giving up and
 * re-enabling anyway. A submission that hangs should not leave the button dead
 * with no way to try again.
 */
const SUBMIT_ACK_TIMEOUT_MS = 30_000

/**
 * Capture descriptor for a source. Photos carry no depth unless told otherwise.
 *
 * `frames` is what a pose solver actually works on: one entry per uploaded image,
 * pointing at it. It is fetched rather than derived because a storage reference
 * is scoped to a viewer and only the server can mint one -- building
 * `sources/<id>/files/<name>` here would produce a ref that is refused, which is
 * the whole point of the scoping.
 *
 * No `intrinsics`, and the capture contract requires them. That requirement was
 * written for a device that knows its own optics; a folder of photographs does
 * not, and a solver estimates them instead. Inventing a plausible focal length
 * would be a number nobody measured, travelling as though somebody had.
 */
async function buildCapture(source: SourceSummary, hasDepth: boolean, hasFiducial: boolean) {
  const capture_id = randomId()

  if (source.kind === 'dicom') {
    return { capture_id, modality: 'radiograph', source_id: source.source_id }
  }

  const listed = await api.listImages(source.source_id)
  const frames = listed.images.map((image) => ({
    id: image.index,
    name: image.name,
    image_ref: image.ref,
  }))

  return {
    capture_id,
    modality: hasDepth ? 'rgbd' : 'rgb',
    source_id: source.source_id,
    frames,
    ...(hasDepth ? { depth: [{ frame_id: 0, depth_ref: 'depth/0', aligned_to_rgb: true }] } : {}),
    quality: {
      coverage_pct: 88,
      ...(hasFiducial ? { scale_reference: { kind: 'aruco', size_mm: 20, detected_frames: [0, 1] } } : {}),
    },
  }
}

/** Stage parameters, read off the result rather than the request: a job restored
 *  after a reload still has its envelope and no longer its submission. */
function stageParams(job: Job, op: string): Record<string, unknown> {
  return job.result?.provenance?.stages?.find((entry) => entry.op === op)?.params ?? {}
}

/** A structure name from the run's own legend, when the segmenter's list is not
 *  in hand. */
function declaredName(job: Job, id: number): string | null {
  return job.result?.segmentation?.classes?.find((entry) => entry.id === id)?.name ?? null
}

/**
 * What a result is called.
 *
 * The stages are already on the row's detail line for anyone who wants them. The
 * name has one job — saying what is on screen — and
 * `isolate_volume → segment → iso_surface` does not say it while "Esophagus"
 * does. So it is taken from what produced the geometry, and named after the
 * structure when the run picked exactly one.
 */
function labelFor(job: Job, names: Map<number, string>): string {
  if (job.name) return job.name

  const ops = new Set(job.ops)

  if (ops.has(Op.ISO_SURFACE)) {
    const chosen = stageParams(job, Op.ISO_SURFACE).label

    if (Array.isArray(chosen)) {
      const wanted = chosen.map(Number).filter((id) => Number.isFinite(id))
      if (wanted.length === 1) {
        return names.get(wanted[0]) ?? declaredName(job, wanted[0]) ?? `Structure ${wanted[0]}`
      }
      if (wanted.length > 1) return `${wanted.length} structures`
    }

    // Every structure the mask holds. Named for the count once the run has one,
    // because "Segmented surface" is the same three words for one structure and
    // for thirty.
    if (chosen === ALL_LABELS) {
      const found = job.result?.segmentation?.classes?.length ?? 0
      return found > 1 ? `${found} structures` : 'Segmented surface'
    }

    const threshold = job.result?.geometry?.threshold
    return threshold != null ? `Surface @ ${Math.round(threshold)} HU` : 'Density surface'
  }

  if (ops.has(Op.RECONSTRUCT)) {
    const solver = job.result?.geometry?.pose_solver
    return solver ? `Point cloud (${solver})` : 'Point cloud'
  }
  if (ops.has(Op.ESTIMATE_POSES)) return 'Camera poses'
  if (ops.has(Op.SEGMENT)) return 'Segmentation'
  if (ops.has(Op.MEASURE)) return 'Measurements'
  if (ops.has(Op.DETECT_CARIES)) return 'Caries detection'
  if (ops.has(Op.PX2TOOTH)) return 'Panoramic teeth'
  if (ops.has(Op.ISOLATE_VOLUME)) return 'Volume'
  return job.ops.join(' → ')
}

export default function EditorRoute() {
  const { activeSource, activeWorkspace, activeSet, refreshSources, selectSource } = useEditor()

  const [selectedOps, setSelectedOps] = useState<string[]>([])
  const [params, setParams] = useState<PipelineParams>({
    threshold: DEFAULT_THRESHOLD,
    stride: 1,
    simplify: DEFAULT_SIMPLIFY_MM,
    labels: null,
    solver: null,
    solverOptions: DEFAULT_SOLVER_OPTIONS,
  })
  const [workflow, setWorkflow] = useState('ct')
  const [hasDepth, setHasDepth] = useState(false)
  const [hasFiducial, setHasFiducial] = useState(false)

  // Results are kept per source so switching away and back does not lose them.
  const [resultsBySource, setResultsBySource] = useState<Record<string, ResultView[]>>({})
  // Live progress, keyed by job id and held apart from the results: it changes
  // several times a second and must not rebuild the result list each time.
  const [progress, setProgress] = useState<Record<string, Progress>>({})
  const [volume, setVolume] = useState<VolumePayload | null>(null)

  // Job ids still being polled. A list rather than a flag: several can be in
  // flight at once, and each row tracks its own.
  const [inFlight, setInFlight] = useState<string[]>([])
  const [segmenter, setSegmenter] = useState<SegmenterClasses | null>(null)
  const [solvers, setSolvers] = useState<SolverEntry[]>([])
  // Held while a submission is in flight, so Run cannot be pressed twice for
  // one job. The backend answers as soon as it has accepted the job.
  const [submitting, setSubmitting] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Polling outlives a render but must not outlive the screen. Set on mount as
  // well as cleared on unmount: StrictMode mounts, unmounts and mounts again in
  // development, and only clearing would leave this false for the real mount —
  // which stops every poller dead and leaves rows saying "queued" for ever.
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  // Progress, on its own socket, into its own state. Kept apart from the results
  // so a bar moving several times a second does not rebuild the result list.
  useEffect(() => subscribeProgress((update) => setProgress((current) => ({ ...current, [update.taskId]: update }))), [])

  const sourceId = activeSource?.source_id ?? null
  const results = sourceId ? (resultsBySource[sourceId] ?? []) : []
  // What can be done here is a property of the data, and the source kind only
  // suggests a default: a panoramic radiograph and a set of intraoral
  // photographs are both image uploads and want different pipelines.
  const ops = activeSource ? workflowFor(workflow).ops : []
  const valueRange = volume?.header.value_range ?? null

  // The volume is fetched here rather than in the viewport because the pipeline
  // threshold needs the same value range to be meaningful.
  useEffect(() => {
    if (!sourceId || activeSource?.kind !== 'dicom' || !activeSource.renderable) {
      setVolume(null)
      return
    }
    let cancelled = false
    api
      .getVolume(sourceId)
      .then((payload) => {
        if (!cancelled) setVolume(payload)
      })
      .catch(() => {
        if (!cancelled) setVolume(null)
      })
    return () => {
      cancelled = true
    }
  }, [sourceId, activeSource?.kind, activeSource?.renderable])

  // Changing source resets the run form and pre-selects that kind's operations.
  useEffect(() => {
    setError(null)
    const suggested = activeSource ? suggestWorkflow(activeSource) : DEFAULT_WORKFLOW.dicom
    setWorkflow(suggested)
    setSelectedOps(activeSource ? workflowFor(suggested).ops : [])
    // Structure names picked for one scan generally do not exist in another,
    // and the failure would only surface once the job had run.
    setParams((current) => ({ ...current, labels: null }))
  }, [sourceId, activeSource?.kind, activeSource?.modality, activeSource?.renderable])

  // Changing workflow changes what the pipeline can do, so the ticks follow it
  // rather than leaving stages selected that this workflow does not offer.
  const changedWorkflow = useRef(true)
  useEffect(() => {
    if (changedWorkflow.current) {
      changedWorkflow.current = false
      return
    }
    setSelectedOps(workflowFor(workflow).ops)
    setParams((current) => ({ ...current, labels: null }))
  }, [workflow])

  // What the segmenter can find, asked for on its own.
  //
  // Not derived from previous results: that made the names unavailable until
  // after a full run, so choosing a structure meant running the pipeline once
  // just to read the list back. The info companion answers in under a second.
  useEffect(() => {
    if (activeSource?.kind !== 'dicom') {
      setSegmenter(null)
      return
    }
    let cancelled = false
    api
      .segmenterClasses()
      .then((found) => {
        if (!cancelled) setSegmenter(found)
      })
      .catch(() => {
        if (!cancelled) setSegmenter(null)
      })
    return () => {
      cancelled = true
    }
  }, [activeSource?.kind])

  // Fetched once per mount rather than per source: what is installed is a
  // property of the deployment, not of the data, so it cannot change by
  // switching sources and re-asking would only add a request per click.
  useEffect(() => {
    let cancelled = false
    api
      .listSolvers()
      .then((found) => {
        if (!cancelled) setSolvers(found.solvers)
      })
      .catch(() => {
        // A deployment that cannot answer this offers no choice, which is the
        // same state as one that has nothing installed.
        if (!cancelled) setSolvers([])
      })
    return () => {
      cancelled = true
    }
  }, [])

  const shape = shapeFor(activeSource?.kind ?? 'images', hasDepth)

  /**
   * Drop a chosen solver the current source cannot support.
   *
   * The choice is made against one set of photographs and outlives them: a
   * sequence solver picked for a capture with depth is not valid for a folder
   * of stills, and the backend would refuse the job after the upload. Cleared on
   * the way in instead. Guarded on the list being loaded, or the empty list
   * before the fetch resolves would clear a perfectly good choice.
   */
  useEffect(() => {
    if (params.solver === null || solvers.length === 0) return
    const usable = solverChoices(solvers, shape)
      .filter((choice) => choice.offered)
      .map((choice) => choice.entry.name)
    if (!usable.includes(params.solver)) {
      setParams((current) => ({ ...current, solver: null }))
    }
  }, [solvers, shape, params.solver])

  // Falls back to names from a run, which is what remains if the info companion
  // is missing — the segmenter can still find structures, it just cannot name
  // them up front.
  const knownLabels = useMemo(() => {
    const declared = segmenter?.classes.map((entry) => entry.name) ?? []
    if (declared.length > 0) return declared
    return results.flatMap((result) =>
      (result.job.result?.segmentation?.classes ?? []).map((entry) => entry.name),
    )
  }, [segmenter, results])

  // Structure id to name, so a surface is called after the structure it is
  // rather than after the stages that produced it.
  const structureNames = useMemo(
    () => new Map<number, string>((segmenter?.classes ?? []).map((entry) => [entry.id, entry.name])),
    [segmenter],
  )

  // Results already on the server, so a refresh does not mean running everything
  // again. The artifacts were always there; only the list was missing.
  useEffect(() => {
    if (!sourceId) return
    let cancelled = false

    api
      .listJobs(50, sourceId)
      .then((jobs) => {
        if (cancelled) return
        setResultsBySource((current) => {
          const existing = current[sourceId] ?? []
          const known = new Set(existing.map((entry) => entry.id))
          // Oldest first, matching how the list is built when submitting.
          const restored = jobs
            .filter((job) => !known.has(job.job_id) && (job.result || job.error))
            .reverse()
            .map<ResultView>((job, index) => ({
              id: job.job_id,
              job,
              label: labelFor(job, structureNames),
              detail: `${job.ops.join(' → ')} · ${job.result?.model_version ?? job.status}`,
              visible: true,
              clipped: true,
              // Coloured by position, so a reloaded page looks like the one
              // that produced them.
              color: surfaceColor(existing.length + index),
            }))
          if (restored.length === 0) return current
          return { ...current, [sourceId]: [...existing, ...restored] }
        })
      })
      .catch(() => {
        // Nothing on the server, or it is unreachable. The screen still works;
        // it just starts empty.
      })

    return () => {
      cancelled = true
    }
  }, [sourceId])

  const updateResults = (id: string, update: (list: ResultView[]) => ResultView[]) =>
    setResultsBySource((current) => ({ ...current, [id]: update(current[id] ?? []) }))

  async function handleFiles(files: File[], kind: 'dicom' | 'images') {
    setUploading(true)
    setError(null)
    try {
      const created =
        kind === 'dicom'
          ? await api.uploadDicom(files, activeWorkspace.id, activeSet.id)
          : await api.uploadImages(files, activeWorkspace.id, activeSet.id)
      await refreshSources()
      selectSource(created.source_id)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setUploading(false)
    }
  }

  async function run() {
    if (submitting || !activeSource || !sourceId) return
    setError(null)
    setSubmitting(true)

    const startedAt = performance.now()
    // A submission that never comes back must not leave the button dead.
    const giveUp = window.setTimeout(() => {
      if (mounted.current) setSubmitting(false)
    }, SUBMIT_ACK_TIMEOUT_MS)

    // The same predicate the form renders on, so a control that is on screen is
    // a control whose value is sent.
    const stages = buildStages(
      ops,
      selectedOps,
      params,
      colmapInPlay(solvers, shape, params.solver),
    )
    const sourceKey = sourceId

    try {
      const submitted = await api.submitJob({
        stages,
        // Recorded on the job, not just held here: two stages under different
        // workflows are different pipelines, and the ops alone do not say which.
        workflow: workflowFor(workflow).label,
        capture: await buildCapture(activeSource, hasDepth, hasFiducial),
      })

      // On screen before the backend has done anything. A segmentation runs for
      // minutes, and a click that appears to do nothing invites a second one.
      updateResults(sourceKey, (list) => [
        ...list,
        {
          id: submitted.job_id,
          job: submitted,
          label: labelFor(submitted, structureNames),
          detail: `${submitted.ops.join(' → ')} · ${submitted.status}`,
          visible: true,
          clipped: true,
          color: surfaceColor(list.length),
        },
      ])
      setInFlight((current) => [...current, submitted.job_id])

      void follow(sourceKey, submitted.job_id)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      window.clearTimeout(giveUp)
      // Held for a beat even when the server answers instantly: a button that
      // flickers back is one the eye cannot tell from a mis-click, and the
      // reflex on a mis-click is to press it again.
      const held = performance.now() - startedAt
      window.setTimeout(
        () => {
          if (mounted.current) setSubmitting(false)
        },
        Math.max(0, SUBMIT_LOCK_MS - held),
      )
    }
  }

  // Jobs that were still running when this page arrived — a refresh mid-run, or
  // another tab. They carry on updating rather than sitting on "running" with
  // nothing left to move them off it. The ref keeps each one to a single poller.
  const resumed = useRef(new Set<string>())
  useEffect(() => {
    if (!sourceId) return
    for (const entry of results) {
      const running = entry.job.status === 'queued' || entry.job.status === 'running'
      if (!running || resumed.current.has(entry.id)) continue
      resumed.current.add(entry.id)
      setInFlight((current) => [...current, entry.id])
      void follow(sourceId, entry.id)
    }
  }, [results, sourceId])

  /**
   * Deletions and renames go to the server, not just to this page.
   *
   * The page re-reads the result list on load, so anything only removed from
   * local state comes back on the next refresh as though nothing happened. The
   * row goes either way: a failed request is not a reason to make the operator
   * click twice, and the next load will put it back if the delete really did
   * not take.
   */
  async function remove(id: string) {
    updateResults(sourceKeyFor(id), (list) => list.filter((entry) => entry.id !== id))
    try {
      await api.deleteJob(id)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }

  async function clearAll() {
    if (!sourceId) return
    const going = results.map((entry) => entry.id)
    updateResults(sourceId, () => [])
    try {
      await Promise.all(going.map((id) => api.deleteJob(id)))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }

  async function rename(id: string, name: string | null) {
    // Shown straight away; the server's answer is what survives a reload.
    updateResults(sourceKeyFor(id), (list) =>
      list.map((entry) =>
        entry.id === id
          ? {
              ...entry,
              job: { ...entry.job, name },
              label: name ?? labelFor({ ...entry.job, name: null }, structureNames),
            }
          : entry,
      ),
    )
    try {
      const updated = await api.renameJob(id, name)
      updateResults(sourceKeyFor(id), (list) =>
        list.map((entry) =>
          entry.id === id
            ? { ...entry, job: updated, label: labelFor(updated, structureNames) }
            : entry,
        ),
      )
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }

  /** Which source a result belongs to, so an update lands in the right list. */
  function sourceKeyFor(id: string): string {
    for (const [key, list] of Object.entries(resultsBySource)) {
      if (list.some((entry) => entry.id === id)) return key
    }
    return sourceId ?? ''
  }

  /**
   * Poll one job to completion, updating its row in place.
   *
   * Deliberately not awaited by `run`: the form stays usable, so more work can
   * be queued behind a long job instead of the whole panel locking up.
   */
  async function follow(sourceKey: string, jobId: string) {
    const settle = () => {
      if (mounted.current) {
        setInFlight((current) => current.filter((id) => id !== jobId))
      }
    }

    const merge = (job: Job) =>
      updateResults(sourceKey, (list) =>
        list.map((entry) =>
          entry.id === jobId
            ? {
                ...entry,
                job,
                detail: `${job.ops.join(' → ')} · ${job.result?.model_version ?? job.status}`,
              }
            : entry,
        ),
      )

    // Both run, and whichever finishes first ends the wait. The poll is not
    // removed -- it is the reason a socket that will not open is not a broken
    // app -- but it drops to a background heartbeat while the stream is up.
    let stopped = false
    const stopWatching = watch(jobId)

    const poll = async () => {
      for (let attempt = 0; attempt < POLL_ATTEMPTS && !stopped; attempt += 1) {
        await new Promise((resolve) =>
          setTimeout(resolve, isLive() ? POLL_FALLBACK_MS : POLL_INTERVAL_MS),
        )
        if (!mounted.current || stopped) return

        const job = await api.getJob(jobId)
        if (!mounted.current) return

        merge(job)
        if (!['queued', 'running'].includes(job.status)) return
      }
    }

    try {
      await Promise.race([settled(jobId), poll()])
    } catch {
      // A dropped poll is not worth a banner over. The row keeps what it last
      // knew and the job is still the backend's; reloading re-reads it.
    } finally {
      stopped = true
      // Unsubscribing also drops any resolver still waiting on this job, so a
      // page that navigates away mid-run leaves nothing behind.
      stopWatching()
      settle()
    }

    // Once, for the finished record. The stream says a job ended; the record is
    // where the result actually is, and one read of it beats two sources of
    // truth for the same thing.
    try {
      const job = await api.getJob(jobId)
      if (mounted.current) merge(job)
    } catch {
      // Left as it was. The next load re-reads it.
    }
  }

  return (
    <div className="editor">
      <MainPanel
        source={activeSource}
        volume={volume}
        results={results}
        busy={inFlight.length > 0}
      />

      <aside className="sidebar">
        <SourceSection onFiles={handleFiles} uploading={uploading} />

        {activeSource?.kind === 'images' && (
          <div className="section">
            <h2 className="section-title">Capture</h2>
            <label className="field">
              <input type="checkbox" checked={hasDepth} onChange={(e) => setHasDepth(e.target.checked)} />
              <span className="field-label">depth available</span>
            </label>
            <label className="field">
              <input
                type="checkbox"
                checked={hasFiducial}
                onChange={(e) => setHasFiducial(e.target.checked)}
              />
              <span className="field-label">fiducial in frame</span>
            </label>
          </div>
        )}

        <ExportSection results={results} />

        <PipelineSection
          running={inFlight.length}
          submitting={submitting}
          ops={ops}
          workflow={workflow}
          onWorkflowChange={setWorkflow}
          selected={selectedOps}
          solvers={solvers}
          shape={shape}
          onToggle={(op) =>
            setSelectedOps((current) => {
              if (current.includes(op)) return current.filter((o) => o !== op)

              const next = [...current, op]
              // A segmentation on its own produces a mask and nothing to look
              // at. The surface is what makes it visible and clickable, so
              // ticking one brings the other — unless the user has already
              // asked for a surface, or this kind has none to offer.
              if (
                op === Op.SEGMENT &&
                !next.includes(SURFACE_AFTER_MASK) &&
                ops.includes(SURFACE_AFTER_MASK)
              ) {
                next.push(SURFACE_AFTER_MASK)
              }
              return next
            })
          }
          params={params}
          onParamsChange={setParams}
          valueRange={valueRange}
          knownLabels={knownLabels}
          onRun={run}
          canRun={Boolean(activeSource)}
        />

        {error && (
          <div className="section">
            <p className="error-text" style={{ margin: 0 }}>
              {error}
            </p>
          </div>
        )}

        {sourceId && (
          <ResultsSection
            results={results}
            sourceKind={activeSource?.kind ?? null}
            workflow={workflowFor(workflow).label}
            progress={progress}
            onToggleVisible={(id) =>
              updateResults(sourceId, (list) =>
                list.map((r) => (r.id === id ? { ...r, visible: !r.visible } : r)),
              )
            }
            onToggleClipped={(id) =>
              updateResults(sourceId, (list) =>
                list.map((r) => (r.id === id ? { ...r, clipped: !r.clipped } : r)),
              )
            }
            onRemove={(id) => void remove(id)}
            onRename={(id, name) => void rename(id, name)}
            onClear={() => void clearAll()}
          />
        )}
      </aside>
    </div>
  )
}
