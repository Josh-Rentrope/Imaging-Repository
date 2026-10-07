import { useEffect, useState } from 'react'

import {
  ALL_LABELS,
  buildStages,
  DEFAULT_THRESHOLD,
  OPS_BY_KIND,
  PipelineSection,
  SURFACE_AFTER_MASK,
  type PipelineParams,
} from '../editor/PipelineSection'
import { ResultsSection, type ResultView } from '../editor/ResultsSection'
import { SourceSection } from '../editor/SourceSection'
import { Viewport3D } from '../editor/Viewport3D'
import { surfaceColor } from '../editor/viewportSettings'
import { api } from '../lib/api'
import { Op, type Job, type SourceSummary, type Stage, type VolumePayload } from '../lib/types'
import { useEditor } from '../state/editor'

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

function labelFor(stages: Stage[], job: Job): string {
  const ops = stages.map((stage) => stage.op)
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
    label: ALL_LABELS,
  })
  const [hasDepth, setHasDepth] = useState(false)
  const [hasFiducial, setHasFiducial] = useState(false)

  // Results are kept per source so switching away and back does not lose them.
  const [resultsBySource, setResultsBySource] = useState<Record<string, ResultView[]>>({})
  const [volume, setVolume] = useState<VolumePayload | null>(null)

  const [busy, setBusy] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const sourceId = activeSource?.source_id ?? null
  const results = sourceId ? (resultsBySource[sourceId] ?? []) : []
  const ops = activeSource ? OPS_BY_KIND[activeSource.kind] : []
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
    setSelectedOps(activeSource ? OPS_BY_KIND[activeSource.kind] : [])
    // A label picked for one scan generally does not exist in another, and the
    // failure would only surface once the job had run.
    setParams((current) => ({ ...current, label: ALL_LABELS }))
  }, [sourceId, activeSource?.kind])

  // Class names from any segmentation already run on this source, so the label
  // field can offer them instead of relying on the user remembering them.
  const knownLabels = results.flatMap((result) =>
    (result.job.result?.segmentation?.classes ?? []).map((entry) => entry.name),
  )

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
    if (!activeSource || !sourceId) return
    setBusy(true)
    setError(null)
    try {
      const stages = buildStages(ops, selectedOps, params)
      let job = await api.submitJob({
        stages,
        capture: buildCapture(activeSource, hasDepth, hasFiducial),
      })
      for (let i = 0; i < 30 && ['queued', 'running'].includes(job.status); i++) {
        await new Promise((resolve) => setTimeout(resolve, 250))
        job = await api.getJob(job.job_id)
      }

      const index = (resultsBySource[sourceId] ?? []).length
      const entry: ResultView = {
        id: job.job_id,
        job,
        label: labelFor(stages, job),
        detail: `${job.ops.join(' → ')} · ${job.result?.model_version ?? job.status}`,
        visible: true,
        clipped: true,
        color: surfaceColor(index),
      }
      updateResults(sourceId, (list) => [...list, entry])
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="editor">
      <Viewport3D source={activeSource} volume={volume} results={results} busy={busy} />

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
          ops={ops}
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
          busy={busy}
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
