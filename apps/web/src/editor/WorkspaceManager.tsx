import { useEffect, useRef, useState } from 'react'

import { useEditor } from '../state/editor'

/**
 * Rename-in-place row.
 *
 * Deletion lives here rather than in the sidebar so the working surface stays
 * free of destructive controls.
 */
function NameRow({
  name,
  detail,
  active,
  deletable,
  onSelect,
  onRename,
  onDelete,
}: {
  name: string
  detail?: string
  active: boolean
  deletable: boolean
  onSelect: () => void
  onRename: (name: string) => void
  onDelete: () => void
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(name)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (editing) inputRef.current?.select()
  }, [editing])

  const commit = () => {
    setEditing(false)
    const cleaned = draft.trim()
    if (cleaned && cleaned !== name) onRename(cleaned)
    else setDraft(name)
  }

  return (
    <div className="source-item" aria-selected={active} onClick={onSelect} role="button" tabIndex={0}>
      {editing ? (
        <input
          ref={inputRef}
          className="source-item-name"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onClick={(event) => event.stopPropagation()}
          onBlur={commit}
          onKeyDown={(event) => {
            if (event.key === 'Enter') commit()
            if (event.key === 'Escape') {
              setDraft(name)
              setEditing(false)
            }
          }}
        />
      ) : (
        <span className="source-item-name">{name}</span>
      )}

      {!editing && detail && <span className="source-item-meta">{detail}</span>}

      {!editing && (
        <span className="row-actions">
          <button
            title="Rename"
            onClick={(event) => {
              event.stopPropagation()
              setDraft(name)
              setEditing(true)
            }}
          >
            ✎
          </button>
          <button
            title={deletable ? 'Delete' : 'Cannot delete the last one'}
            disabled={!deletable}
            onClick={(event) => {
              event.stopPropagation()
              onDelete()
            }}
          >
            ✕
          </button>
        </span>
      )}
    </div>
  )
}

export function WorkspaceManager({ open, onClose }: { open: boolean; onClose: () => void }) {
  const {
    workspaces,
    activeWorkspace,
    activeSet,
    sources,
    selectWorkspace,
    addWorkspace,
    renameWorkspace,
    deleteWorkspace,
    selectSet,
    addSet,
    renameSet,
    deleteSet,
    renameSource,
    deleteSource,
  } = useEditor()

  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(event) => event.stopPropagation()} role="dialog" aria-modal="true">
        <header className="modal-header">
          <h2>Workspaces and sets</h2>
          <button onClick={onClose}>close</button>
        </header>

        <div className="modal-body">
          <section className="modal-column">
            <div className="section-title">
              Workspaces
              <button onClick={() => addWorkspace(`Workspace ${workspaces.length + 1}`)}>+</button>
            </div>
            {workspaces.map((workspace) => (
              <NameRow
                key={workspace.id}
                name={workspace.name}
                detail={`${workspace.sets.length} set${workspace.sets.length === 1 ? '' : 's'}`}
                active={workspace.id === activeWorkspace.id}
                deletable={workspaces.length > 1}
                onSelect={() => selectWorkspace(workspace.id)}
                onRename={(name) => renameWorkspace(workspace.id, name)}
                onDelete={() => deleteWorkspace(workspace.id)}
              />
            ))}
          </section>

          <section className="modal-column">
            <div className="section-title">
              Sets in {activeWorkspace.name}
              <button onClick={() => addSet(`Working set ${activeWorkspace.sets.length + 1}`)}>+</button>
            </div>
            {activeWorkspace.sets.map((set) => (
              <NameRow
                key={set.id}
                name={set.name}
                active={set.id === activeSet.id}
                deletable={activeWorkspace.sets.length > 1}
                onSelect={() => selectSet(set.id)}
                onRename={(name) => renameSet(set.id, name)}
                onDelete={() => deleteSet(set.id)}
              />
            ))}
          </section>

          <section className="modal-column">
            <div className="section-title">
              Sources in {activeSet.name}
              <span className="muted">{sources.length}</span>
            </div>
            {sources.length === 0 ? (
              <p className="muted">Nothing in this working set.</p>
            ) : (
              sources.map((source) => (
                <NameRow
                  key={source.source_id}
                  name={source.name}
                  detail={source.kind}
                  active={false}
                  deletable
                  onSelect={() => undefined}
                  onRename={(name) => void renameSource(source.source_id, name)}
                  onDelete={() => {
                    if (window.confirm(`Delete "${source.name}"? The uploaded files are removed.`)) {
                      void deleteSource(source.source_id)
                    }
                  }}
                />
              ))
            )}
          </section>
        </div>
      </div>
    </div>
  )
}
