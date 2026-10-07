import { useEffect, useState } from 'react'

import { OPS_BY_KIND, PipelineSection } from '../editor/PipelineSection'
import { ResultSection } from '../editor/ResultSection'
import { SourceSection } from '../editor/SourceSection'
import { Viewport3D } from '../editor/Viewport3D'
import { api } from '../lib/api'
import type { Job, SourceSummary, Stage } from '../lib/types'
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

export default function EditorRoute() {
  const { activeSource, activeWorkspace, activeSet, refreshSources, selectSource } = useEditor()

  const [selectedOps, setSelectedOps] = useState<string[]>([])
  const [hasDepth, setHasDepth] = useState(false)
  const [hasFiducial, setHasFiducial] = useState(false)

  const [job, setJob] = useState<Job | null>(null)
  const [busy, setBusy] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const ops = activeSource ? OPS_BY_KIND[activeSource.kind] : []

  // Changing source resets the run and pre-selects that kind's operations.
  useEffect(() => {
    setJob(null)
    setError(null)
    setSelectedOps(activeSource ? OPS_BY_KIND[activeSource.kind] : [])
  }, [activeSource?.source_id, activeSource?.kind])

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
    if (!activeSource) return
    setBusy(true)
    setError(null)
    setJob(null)
    try {
      const stages: Stage[] = selectedOps.map((op) => ({ op }))
      let current = await api.submitJob({
        stages,
        capture: buildCapture(activeSource, hasDepth, hasFiducial),
      })
      for (let i = 0; i < 20 && ['queued', 'running'].includes(current.status); i++) {
        await new Promise((resolve) => setTimeout(resolve, 250))
        current = await api.getJob(current.job_id)
      }
      setJob(current)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="editor">
      <Viewport3D source={activeSource} result={job?.result ?? null} job={job} busy={busy} />

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
            setSelectedOps((current) =>
              current.includes(op) ? current.filter((o) => o !== op) : [...current, op],
            )
          }
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

        <ResultSection job={job} />
      </aside>
    </div>
  )
}
