import { useEffect, useMemo, useRef, useState } from 'react'

import {
  buildStages,
  DEFAULT_SIMPLIFY_MM,
  DEFAULT_THRESHOLD,
  DEFAULT_WORKFLOW,
  PipelineSection,
  SURFACE_AFTER_MASK,
  workflowFor,
  type PipelineParams,
} from '../editor/PipelineSection'
import { ResultsSection, type ResultView } from '../editor/ResultsSection'
import { SourceSection } from '../editor/SourceSection'
import { MainPanel } from '../editor/MainPanel'
import { surfaceColor } from '../editor/viewportSettings'
import { api } from '../lib/api'
import {
  Op,
  type Job,
  type SegmenterClasses,
  type SourceSummary,
  type VolumePayload,
} from '../lib/types'
import { useEditor } from '../state/editor'

//: A segmentation on a full CT runs for minutes, so the wait is budgeted in tens
//: of minutes rather than the seconds a marching-cubes pass takes.
const POLL_INTERVAL_MS = 1500
const POLL_ATTEMPTS = 800

/** How long Run stays disabled after a click, whatever the server does. */
const SUBMIT_LOCK_MS = 1000

/**
 * How long Run waits for the server to acknowledge before giving up and
 * re-enabling anyway. A submission that hangs should not leave the button dead
 * with no way to try again.
 */
const SUBMIT_ACK_TIMEOUT_MS = 30_000

/** Capture descriptor for a source. Photos carry no depth unless told otherwise. */
function buildCapture(source: SourceSummary, hasDepth: boolean, hasFiducial: boolean) {
  const capture_id = crypto.randomUUID()

  if (source.kind === 'dicom') {
    return { capture_id, modality: 'radiograph', source_id: source.source_id }
  }

  return {
    capture_id,
    modality: hasDepth ? 'rgbd' : 'rgb',
    source_id: source.source_id,
    ...(hasDepth ? { depth: [{ frame_id: 0, depth_ref: 'depth/0', aligned_to_rgb: true }] } : {}),
    quality: {
      coverage_pct: 88,
      ...(hasFiducial ? { scale_reference: { kind: 'aruco', size_mm: 20, detected_frames: [0, 1] } } : {}),
    },
  }
}

function labelFor(ops: string[], job: Job): string {
  if (ops.length === 1 && ops[0] === Op.ISO_SURFACE) {
    const threshold = job.result?.geometry?.threshold
    return threshold != null ? `Surface @ ${Math.round(threshold)}` : 'Surface'
  }
  return ops.join(' → ')
}

export default function EditorRoute() {
  const { activeSource, activeWorkspace, activeSet, refreshSources, selectSource } = useEditor()

  const [selectedOps, setSelectedOps] = useState<string[]>([])
  const [params, setParams] = useState<PipelineParams>({
    threshold: DEFAULT_THRESHOLD,
    stride: 1,
    simplify: DEFAULT_SIMPLIFY_MM,
    labels: null,
  })
  const [workflow, setWorkflow] = useState('ct')
  const [hasDepth, setHasDepth] = useState(false)
  const [hasFiducial, setHasFiducial] = useState(false)

  // Results are kept per source so switching away and back does not lose them.
  const [resultsBySource, setResultsBySource] = useState<Record<string, ResultView[]>>({})
  const [volume, setVolume] = useState<VolumePayload | null>(null)

  // Job ids still being polled. A list rather than a flag: several can be in
  // flight at once, and each row tracks its own.
  const [inFlight, setInFlight] = useState<string[]>([])
  const [segmenter, setSegmenter] = useState<SegmenterClasses | null>(null)
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
    const suggested = activeSource ? DEFAULT_WORKFLOW[activeSource.kind] : 'ct'
    setWorkflow(suggested)
    setSelectedOps(activeSource ? workflowFor(suggested).ops : [])
    // Structure names picked for one scan generally do not exist in another,
    // and the failure would only surface once the job had run.
    setParams((current) => ({ ...current, labels: null }))
  }, [sourceId, activeSource?.kind])

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
              label: labelFor(job.ops, job),
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

    const stages = buildStages(ops, selectedOps, params)
    const sourceKey = sourceId

    try {
      const submitted = await api.submitJob({
        stages,
        capture: buildCapture(activeSource, hasDepth, hasFiducial),
      })

      // On screen before the backend has done anything. A segmentation runs for
      // minutes, and a click that appears to do nothing invites a second one.
      updateResults(sourceKey, (list) => [
        ...list,
        {
          id: submitted.job_id,
          job: submitted,
          label: labelFor(submitted.ops, submitted),
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

    try {
      for (let attempt = 0; attempt < POLL_ATTEMPTS; attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS))
        if (!mounted.current) return

        const job = await api.getJob(jobId)
        if (!mounted.current) return

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

        if (!['queued', 'running'].includes(job.status)) return
      }
    } catch {
      // A dropped poll is not worth a banner over. The row keeps what it last
      // knew and the job is still the backend's; reloading re-reads it.
    } finally {
      settle()
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

        <PipelineSection
          running={inFlight.length}
          submitting={submitting}
          ops={ops}
          workflow={workflow}
          onWorkflowChange={setWorkflow}
          selected={selectedOps}
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
            onRemove={(id) =>
              updateResults(sourceId, (list) => list.filter((r) => r.id !== id))
            }
            onClear={() => updateResults(sourceId, () => [])}
          />
        )}
      </aside>
    </div>
  )
}
