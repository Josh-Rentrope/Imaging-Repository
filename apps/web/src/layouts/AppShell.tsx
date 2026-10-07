import { useState, type ReactNode } from 'react'

import { BackendStatus } from '../components/BackendStatus'
import { WorkspaceManager } from '../editor/WorkspaceManager'
import { useEditor } from '../state/editor'

/**
 * Application frame.
 *
 * The workspace switcher scopes everything below it and the working-set
 * switcher picks the collection of sources the editor operates on. Renaming and
 * deletion live in the manager rather than beside the switchers, so switching
 * stays a two-click operation with nothing destructive adjacent to it.
 */
export function AppShell({ children }: { children: ReactNode }) {
  const { workspaces, activeWorkspaceId, selectWorkspace, activeWorkspace, activeSet, selectSet } =
    useEditor()
  const [managerOpen, setManagerOpen] = useState(false)

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

        <span className="header-spacer" />
        <BackendStatus />
      </header>

      {children}

      <WorkspaceManager open={managerOpen} onClose={() => setManagerOpen(false)} />
    </div>
  )
}
