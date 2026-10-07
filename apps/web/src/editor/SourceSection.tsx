import { useEditor, type Source } from '../state/editor'
import { DropZone } from './DropZone'

function SourceRow({ source, selected, onSelect }: { source: Source; selected: boolean; onSelect: () => void }) {
  return (
    <li>
      <button
        className="source-item"
        aria-selected={selected}
        onClick={onSelect}
        title={source.error ?? source.name}
      >
        <span className="source-item-name">{source.name}</span>
        <span className="source-item-meta">
          {source.status === 'error' ? <span className="error-text">!</span> : source.meta}
        </span>
      </button>
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
  const { activeSources, activeSourceId, selectSource, clearSources } = useEditor()

  return (
    <div className="section">
      <h2 className="section-title">
        Sources
        {activeSources.length > 0 && (
          <span className="section-count">
            {activeSources.length}
            <button
              onClick={clearSources}
              title="Remove all sources from this working set"
              style={{ marginLeft: 8, padding: '0 6px' }}
            >
              clear
            </button>
          </span>
        )}
      </h2>

      <DropZone onFiles={onFiles} disabled={uploading} />

      {activeSources.length > 0 && (
        <ul className="source-list" style={{ marginTop: 8 }}>
          {activeSources.map((source) => (
            <SourceRow
              key={source.id}
              source={source}
              selected={source.id === activeSourceId}
              onSelect={() => selectSource(source.id)}
            />
          ))}
        </ul>
      )}
    </div>
  )
}
