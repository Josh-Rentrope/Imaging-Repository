import { useState, type ReactNode } from 'react'

import { BackendStatus } from '../components/BackendStatus'
import { SamplesModal } from '../editor/SamplesModal'
import { WorkspaceManager } from '../editor/WorkspaceManager'
import { useEditor } from '../state/editor'
import type { ImportedSource } from '../lib/types'

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
    pending
      .then(() => refreshSources())
      .catch((cause: unknown) => {
        setImportError(cause instanceof Error ? cause.message : String(cause))
      })
      .finally(() => setImporting(null))
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
          {importing && <span className="muted import-status">fetching…</span>}
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
