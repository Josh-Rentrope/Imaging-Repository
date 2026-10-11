import { useEffect, useState, type ReactNode } from 'react'

import { BackendStatus } from '../components/BackendStatus'
import { ProgressBar } from '../components/ProgressBar'
import { SamplesModal } from '../editor/SamplesModal'
import { WorkspaceManager } from '../editor/WorkspaceManager'
import { useEditor } from '../state/editor'
import { VIEWER_TOPIC, subscribeProgress, watchTopic, type Progress } from '../lib/stream'
import type { ImportedSource } from '../lib/types'

/**
 * What the server calls a viewer-scoped transfer. Shared with
 * `app/progress.py::VIEWER_TASK`, and the only thing the two sides have to agree
 * on for a download that is not a job.
 */
const VIEWER_TASK = 'download'

/**
 * Application frame.
 *
 * The workspace switcher scopes everything below it and the working-set
 * switcher picks the collection of sources the editor operates on. Renaming and
 * deletion live in the manager rather than beside the switchers, so switching
 * stays a two-click operation with nothing destructive adjacent to it.
 */
export function AppShell({ children }: { children: ReactNode }) {
  const {
    workspaces,
    activeWorkspaceId,
    selectWorkspace,
    activeWorkspace,
    activeSet,
    selectSet,
    refreshSources,
  } = useEditor()
  const [managerOpen, setManagerOpen] = useState(false)
  const [samplesOpen, setSamplesOpen] = useState(false)
  const [importing, setImporting] = useState<string | null>(null)
  const [importError, setImportError] = useState<string | null>(null)
  // Live progress for the viewer's own transfers. Its own state rather than
  // folded into `importing`, because it changes several times a second and the
  // header should not rebuild on every tick.
  const [download, setDownload] = useState<Progress | null>(null)

  useEffect(
    () =>
      subscribeProgress((update) => {
        if (update.taskId === VIEWER_TASK) setDownload(update)
      }),
    [],
  )

  /**
   * An import that outlives the modal that started it.
   *
   * The modal closes on submit, so the request has nobody waiting on it by the
   * time it resolves. The failure has to be surfaced here — silently dropping it
   * would leave someone who clicked "add" with no source and no explanation,
   * which reads as a button that does nothing.
   */
  function onImportStarted(pending: Promise<ImportedSource>, label: string) {
    setImporting(label)
    // Subscribed before the request goes out, so the bar is already watching
    // when the first byte arrives. A download has no job id to be watched by;
    // it reports on the viewer's own topic instead.
    const stop = watchTopic(VIEWER_TOPIC)
    pending
      .then(() => refreshSources())
      .catch((cause: unknown) => {
        setImportError(cause instanceof Error ? cause.message : String(cause))
      })
      .finally(() => {
        stop()
        setImporting(null)
        // The server forgets it; this side has to as well, or the header keeps a
        // bar at 40% for a download that finished.
        setDownload(null)
      })
  }

  return (
    <div className="app">
      <header className="header">
        <span className="brand">Bone Viewer</span>

        <div className="header-group">
          <select
            value={activeWorkspaceId}
            onChange={(event) => selectWorkspace(event.target.value)}
            title="Workspace"
          >
            {workspaces.map((workspace) => (
              <option key={workspace.id} value={workspace.id}>
                {workspace.name}
              </option>
            ))}
          </select>
          <span className="muted">/</span>
          <select value={activeSet.id} onChange={(event) => selectSet(event.target.value)} title="Working set">
            {activeWorkspace.sets.map((set) => (
              <option key={set.id} value={set.id}>
                {set.name}
              </option>
            ))}
          </select>
          <button onClick={() => setManagerOpen(true)} title="Manage workspaces, sets and sources">
            manage
          </button>
        </div>

        <div className="header-group">
          <button onClick={() => setSamplesOpen(true)} title="Add a sample dataset">
            samples
          </button>
          {/* The modal is gone by now, so the progress has to live somewhere. */}
          {importing && (
            <span className="import-status" style={{ minWidth: 180 }}>
              {/* Bytes from the server, which is the half that takes minutes.
                  Unpacking and staging are local and fast by comparison, and
                  have no size to report against. */}
              <ProgressBar
                label={download?.label || 'fetching…'}
                fraction={download?.fraction ?? null}
                detail={
                  download?.done != null && download.total
                    ? `${Math.round((download.done / download.total) * 100)}%`
                    : undefined
                }
              />
            </span>
          )}
        </div>

        <span className="header-spacer" />
        <BackendStatus />
      </header>

      {importError && (
        <div className="banner error-banner">
          <span>
            That dataset could not be added: {importError}
          </span>
          <button className="ghost" onClick={() => setImportError(null)} aria-label="Dismiss">
            ✕
          </button>
        </div>
      )}

      {children}

      <WorkspaceManager open={managerOpen} onClose={() => setManagerOpen(false)} />

      <SamplesModal
        open={samplesOpen}
        onClose={() => setSamplesOpen(false)}
        workspaceId={activeWorkspace.id}
        setId={activeSet.id}
        onImportStarted={onImportStarted}
      />
    </div>
  )
}
