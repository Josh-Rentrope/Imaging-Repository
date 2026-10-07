import { type ReactNode } from 'react'

import { BackendStatus } from '../components/BackendStatus'
import { useEditor } from '../state/editor'

/**
 * Application frame.
 *
 * The workspace switcher scopes everything below it; the working-set switcher
 * picks the collection of sources the editor is operating on. Both are native
 * selects — swap for a menu component when the styling pass happens.
 */
export function AppShell({ children }: { children: ReactNode }) {
  const {
    workspaces,
    activeWorkspaceId,
    selectWorkspace,
    addWorkspace,
    activeWorkspace,
    activeSet,
    selectSet,
    addSet,
  } = useEditor()

  const nameFor = (fallback: string) => {
    const entered = window.prompt('Name', fallback)
    return entered?.trim() || null
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
          <button
            title="New workspace"
            onClick={() => {
              const name = nameFor(`Workspace ${workspaces.length + 1}`)
              if (name) addWorkspace(name)
            }}
          >
            +
          </button>
        </div>

        <div className="header-group">
          <select
            value={activeSet.id}
            onChange={(event) => selectSet(event.target.value)}
            title="Working set"
          >
            {activeWorkspace.sets.map((set) => (
              <option key={set.id} value={set.id}>
                {set.name}
              </option>
            ))}
          </select>
          <button
            title="New working set"
            onClick={() => {
              const name = nameFor(`Working set ${activeWorkspace.sets.length + 1}`)
              if (name) addSet(name)
            }}
          >
            +
          </button>
        </div>

        <span className="header-spacer" />
        <BackendStatus />
      </header>

      {children}
    </div>
  )
}
