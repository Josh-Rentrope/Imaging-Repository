import { useEffect, useMemo, useState } from 'react'

import { api } from '../lib/api'
import type { ImportedSource, SampleEntry, SamplesCatalogue, SourceKind } from '../lib/types'
import { Info } from './Info'

/**
 * Fetch a dataset the app does not have locally.
 *
 * The warning is not decoration. This makes the *server* open a connection
 * wherever the URL points and unpack whatever comes back, so the honest
 * description of the risk is "the server will fetch and extract a file from a
 * stranger", which is worth one sentence before the button.
 *
 * Imports do not block the modal. A series zip is tens of megabytes, so the
 * modal closes as soon as the request is away and the source appears in the
 * list when it lands — waiting in a dialog to watch a spinner is not a better
 * use of the wait.
 */

/** How the server is asked to read the archive. */
export type KindChoice = 'auto' | SourceKind

const KIND_LABELS: Record<KindChoice, string> = {
  auto: 'detect from the contents',
  dicom: 'DICOM series',
  images: 'photographs (SfM)',
}

/** A study's size, said usefully. */
function sizeLabel(sample: SampleEntry): string {
  if (!sample.bytes) return ''
  // One decimal below 10 MB: four of the studies here round to "0 MB" at whole
  // numbers, which reads as a broken entry rather than a small one.
  const mb = sample.bytes / 1_000_000
  return mb < 10 ? `${mb.toFixed(1)} MB` : `${Math.round(mb)} MB`
}

export function SamplesModal({
  open,
  onClose,
  onImportStarted,
  workspaceId,
  setId,
}: {
  open: boolean
  onClose: () => void
  /** Handed the in-flight request, so the caller can refresh when it lands. */
  onImportStarted: (pending: Promise<ImportedSource>, label: string) => void
  workspaceId: string
  setId: string
}) {
  const [catalogue, setCatalogue] = useState<SamplesCatalogue | null>(null)
  const [url, setUrl] = useState('')
  const [name, setName] = useState('')
  const [kind, setKind] = useState<KindChoice>('auto')
  const [modality, setModality] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!open) return
    api
      .listSamples()
      .then(setCatalogue)
      .catch(() => setCatalogue(null))
  }, [open])

  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    globalThis.addEventListener('keydown', onKey)
    return () => globalThis.removeEventListener('keydown', onKey)
  }, [open, onClose])

  const samples = catalogue?.samples ?? []
  const modalities = catalogue?.modalities ?? []

  const shown = useMemo(
    () => (modality ? samples.filter((sample) => sample.modality === modality) : samples),
    [samples, modality],
  )

  function importFrom(source: string, label: string) {
    setBusy(true)
    const request = api.importRemote({
      url: source,
      workspace_id: workspaceId,
      set_id: setId,
      ...(label ? { name: label } : {}),
      // Omitted rather than sent as a word the server has to interpret: absent
      // is the single spelling of "you decide".
      ...(kind === 'auto' ? {} : { kind }),
    })
    // Handed off before it resolves, then the modal goes away. The rejection has
    // to be swallowed here or it surfaces as an unhandled rejection; the caller
    // is told about the failure through the same promise it is given.
    request.catch(() => undefined).finally(() => setBusy(false))
    onImportStarted(request, label || source)
    onClose()
  }

  if (!open) return null

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal modal-wide"
        onClick={(event) => event.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="Sample datasets"
      >
        <header className="modal-header">
          <h2>Sample datasets</h2>
          <button className="ghost" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>

        <div className="modal-body column">
          <p className="muted modal-lede">
            Point at a direct link to a <span className="mono">.zip</span>,{' '}
            <span className="mono">.tar.gz</span> or single file. The server downloads it and
            unpacks it into a source you can use like any upload.
            <Info
              text={
                'The server opens a connection to whatever host the link names and extracts what ' +
                'comes back. Only public addresses are accepted — internal ones are refused — but ' +
                'a link is still a file from somewhere else, so use ones you trust. RAR is not ' +
                'supported: it needs the unrar tool, which is not installed here.'
              }
            />
          </p>

          <div className="import-row">
            <label className="field grow">
              <span className="field-label">URL</span>
              <input
                className="text-input mono"
                type="url"
                placeholder="https://…/dataset.zip"
                value={url}
                onChange={(event) => setUrl(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' && url.trim()) importFrom(url.trim(), name.trim())
                }}
              />
            </label>

            <label className="field">
              <span className="field-label">read as</span>
              <select
                value={kind}
                onChange={(event) => setKind(event.target.value as KindChoice)}
                title="How the server should treat what it unpacks"
              >
                {Object.entries(KIND_LABELS).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>

            <label className="field">
              <span className="field-label">name</span>
              <input
                className="text-input"
                type="text"
                placeholder="optional"
                value={name}
                onChange={(event) => setName(event.target.value)}
              />
            </label>
          </div>

          <p className="popover-hint warn-text" style={{ margin: '2px 0 12px' }}>
            Whatever that link returns will be downloaded and extracted on the server.
            <Info
              text={
                'Detection looks for a DICOM preamble or a recognisable image header in the first ' +
                'few files. When the archive is mixed, or holds neither, it says so on the result ' +
                'rather than guessing silently — override it here if you already know.'
              }
            />
          </p>

          <div className="controls">
            <button
              className="primary"
              disabled={busy || !url.trim()}
              onClick={() => importFrom(url.trim(), name.trim())}
            >
              Fetch and add
            </button>
          </div>

          <h3 className="section-title samples-heading">
            {modality ? `${modality} studies` : 'Known good'}
            <span className="muted samples-count">
              {shown.length} of {samples.length}
            </span>
          </h3>

          {modalities.length > 1 && (
            <div className="chip-row">
              <button
                className={`chip${modality === null ? ' chip-on' : ''}`}
                onClick={() => setModality(null)}
              >
                all
              </button>
              {modalities.map((entry) => (
                <button
                  key={entry}
                  className={`chip${modality === entry ? ' chip-on' : ''}`}
                  onClick={() => setModality(entry)}
                >
                  {entry}
                </button>
              ))}
            </div>
          )}

          {samples.length === 0 ? (
            // Said plainly rather than shown as an empty list: "nothing here"
            // plus the reason is more useful than a blank panel that reads as a
            // loading failure.
            <p className="muted" style={{ margin: 0 }}>
              {catalogue?.configured
                ? 'The catalogue is configured but has no entries yet.'
                : 'No catalogue is configured. Set BONE_VIEWER_SAMPLE_BASE to a bucket of demo datasets, or add entries to app/samples.py.'}
            </p>
          ) : (
            <ul className="sample-grid">
              {shown.map((sample) => (
                <li key={sample.id} className="sample-card">
                  <div className="sample-card-head">
                    <span className="sample-title">{sample.title}</span>
                    <button
                      className="ghost"
                      disabled={busy || !sample.available}
                      title={sample.available ? 'Fetch this dataset' : 'No URL configured'}
                      onClick={() => importFrom(sample.url, sample.title)}
                    >
                      add
                    </button>
                  </div>

                  <p className="sample-desc">{sample.description}</p>

                  <div className="sample-meta">
                    {sample.modality && <span className="tag">{sample.modality}</span>}
                    {sample.anatomy && <span>{sample.anatomy}</span>}
                    {sizeLabel(sample) && <span className="mono">{sizeLabel(sample)}</span>}
                    <span className="solver-licence">{sample.licence}</span>
                  </div>

                  {/* CC BY is commercial use *with attribution*, so the credit is
                      part of the offer rather than a footnote to it. */}
                  {sample.collection && (
                    <div className="sample-credit">
                      {sample.doi ? (
                        <a href={sample.doi} target="_blank" rel="noreferrer">
                          {sample.collection}
                        </a>
                      ) : (
                        <span>{sample.collection}</span>
                      )}
                      {sample.source && (
                        <>
                          {' · '}
                          <a href={sample.source} target="_blank" rel="noreferrer">
                            source
                          </a>
                        </>
                      )}
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  )
}
