import { useEffect, useRef, useState } from 'react'

import type { SourceSummary } from '../lib/types'
import { useEditor } from '../state/editor'
import { DropZone } from './DropZone'
import { Section } from './Section'

function meta(source: SourceSummary): string {
  if (source.kind === 'dicom') return `${source.instance_count} slice${source.instance_count === 1 ? '' : 's'}`
  return `${source.instance_count} image${source.instance_count === 1 ? '' : 's'}`
}

function SourceRow({ source, selected }: { source: SourceSummary; selected: boolean }) {
  const { selectSource, renameSource } = useEditor()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(source.name)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (editing) inputRef.current?.select()
  }, [editing])

  const commit = () => {
    setEditing(false)
    const cleaned = draft.trim()
    if (cleaned && cleaned !== source.name) void renameSource(source.source_id, cleaned)
    else setDraft(source.name)
  }

  return (
    <li>
      <div
        className="source-item"
        aria-selected={selected}
        onClick={() => selectSource(source.source_id)}
        onDoubleClick={() => {
          setDraft(source.name)
          setEditing(true)
        }}
        role="button"
        tabIndex={0}
        onKeyDown={(event) => {
          if (event.key === 'Enter') selectSource(source.source_id)
        }}
        title={source.render_reason ?? source.original_name ?? source.name}
      >
        {editing ? (
          <input
            ref={inputRef}
            className="source-item-name"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onBlur={commit}
            onClick={(event) => event.stopPropagation()}
            onKeyDown={(event) => {
              if (event.key === 'Enter') commit()
              if (event.key === 'Escape') {
                setDraft(source.name)
                setEditing(false)
              }
            }}
          />
        ) : (
          <span className="source-item-name">{source.name}</span>
        )}

        {!editing && (
          <span className="source-item-meta">
            {!source.renderable && <span title={source.render_reason ?? undefined}>·</span>} {meta(source)}
          </span>
        )}

        {selected && !editing && (
          <button
            className="source-item-action"
            title="Rename"
            onClick={(event) => {
              event.stopPropagation()
              setDraft(source.name)
              setEditing(true)
            }}
          >
            ✎
          </button>
        )}
      </div>
    </li>
  )
}

export function SourceSection({
  onFiles,
  uploading,
}: {
  onFiles: (files: File[], kind: 'dicom' | 'images') => void
  uploading: boolean
}) {
  const { sources, sourcesLoading, sourcesError, activeSourceId } = useEditor()

  return (
    <Section id="sources" title="Sources" count={sources.length > 0 ? sources.length : undefined}>
      <DropZone onFiles={onFiles} disabled={uploading} />

      {uploading && <p className="muted" style={{ margin: '8px 0 0' }}>Uploading…</p>}
      {sourcesError && <p className="error-text" style={{ margin: '8px 0 0' }}>{sourcesError}</p>}

      {sourcesLoading ? (
        <p className="muted" style={{ margin: '8px 0 0' }}>Loading…</p>
      ) : sources.length > 0 ? (
        <ul className="source-list" style={{ marginTop: 8 }}>
          {sources.map((source) => (
            <SourceRow key={source.source_id} source={source} selected={source.source_id === activeSourceId} />
          ))}
        </ul>
      ) : (
        <p className="muted" style={{ margin: '8px 0 0' }}>Nothing in this working set.</p>
      )}

      {sources.length > 0 && (
        <p className="muted" style={{ margin: '8px 0 0', fontSize: 11 }}>
          Double-click a name to rename.
        </p>
      )}
    </Section>
  )
}
