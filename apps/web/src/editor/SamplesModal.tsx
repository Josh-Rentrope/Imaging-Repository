import { useEffect, useState } from 'react'

import { api } from '../lib/api'
import type { SampleEntry, SamplesCatalogue, SourceSummary } from '../lib/types'
import { Info } from './Info'

/**
 * Fetch a dataset the app does not have locally.
 *
 * The warning is not decoration. This makes the *server* open a connection
 * wherever the URL points and unpack whatever comes back, so the honest
 * description of the risk is "the server will fetch and extract a file from a
 * stranger", which is worth one sentence before the button.
 */
export function SamplesModal({
  open,
  onClose,
  onImported,
  workspaceId,
  setId,
}: {
  open: boolean
  onClose: () => void
  onImported: (source: SourceSummary) => void
  workspaceId: string
  setId: string
}) {
  const [catalogue, setCatalogue] = useState<SamplesCatalogue | null>(null)
  const [url, setUrl] = useState('')
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    setError(null)
    setDone(null)
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

  async function importFrom(source: string, label: string) {
    setBusy(true)
    setError(null)
    setDone(null)
    try {
      const created = await api.importRemote({
        url: source,
        workspace_id: workspaceId,
        set_id: setId,
        ...(label ? { name: label } : {}),
      })
      setDone(`${created.name} — ${created.file_count} files`)
      setUrl('')
      setName('')
      onImported(created)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
    }
  }

  if (!open) return null

  const samples: SampleEntry[] = catalogue?.samples ?? []

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal modal-narrow"
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
          <p className="muted" style={{ marginTop: 0 }}>
            Point at a direct link to a <span className="mono">.zip</span>,{' '}
            <span className="mono">.tar.gz</span> or single file. The server downloads it and
            unpacks it into a source you can use like any upload.
            <Info
              text={
                'The server opens a connection to whatever host the link names and extracts what comes back. ' +
                'Only public addresses are accepted — internal ones are refused — but a link is still a file ' +
                'from somewhere else, so use ones you trust. RAR is not supported: it needs the unrar tool, ' +
                'which is not installed here.'
              }
            />
          </p>

          <label className="field">
            <span className="field-label">URL</span>
            <input
              className="text-input mono"
              type="url"
              placeholder="https://…/dataset.zip"
              value={url}
              onChange={(event) => setUrl(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && url.trim()) void importFrom(url.trim(), name.trim())
              }}
            />
          </label>

          <label className="field">
            <span className="field-label">name</span>
            <input
              className="text-input"
              type="text"
              placeholder="optional — defaults to the file name"
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
          </label>

          <p className="popover-hint warn-text" style={{ margin: '0 0 8px' }}>
            Whatever that link returns will be downloaded and extracted on the server.
          </p>

          <div className="controls">
            <button
              className="primary"
              disabled={busy || !url.trim()}
              onClick={() => void importFrom(url.trim(), name.trim())}
            >
              {busy ? 'Fetching…' : 'Fetch and add'}
            </button>
          </div>

          {error && (
            <p className="error-text" style={{ margin: '8px 0 0', fontSize: 11 }}>
              {error}
            </p>
          )}
          {done && (
            <p className="muted" style={{ margin: '8px 0 0', fontSize: 11 }}>
              Added {done}.
            </p>
          )}

          <h3 className="section-title" style={{ marginTop: 18 }}>
            Known good
          </h3>

          {samples.length === 0 ? (
            // Said plainly rather than shown as an empty list: the catalogue is
            // empty on purpose, and "nothing here" plus the reason is more
            // useful than a blank panel that reads as a loading failure.
            <p className="muted" style={{ margin: 0 }}>
              {catalogue?.configured
                ? 'The catalogue is configured but has no entries yet.'
                : 'No catalogue is configured for this deployment. Set BONE_VIEWER_SAMPLE_BASE to a bucket of demo datasets and list them in app/samples.py.'}
              <Info text="Almost every public photogrammetry set is licensed for non-commercial research only — the openMVG image sets, which every structure-from-motion tutorial uses, are CC BY-NC 4.0. A product that ships a curated list of them is recommending them, which is the part that carries the exposure. So the catalogue points at a bucket we control." />
            </p>
          ) : (
            <ul className="source-list">
              {samples.map((sample) => (
                <li key={sample.id} className="result-item">
                  <div className="result-row">
                    <span className="source-item-name">{sample.title}</span>
                    <button
                      className="ghost"
                      disabled={busy || !sample.available}
                      title={sample.available ? 'Fetch this dataset' : 'No URL configured'}
                      onClick={() => void importFrom(sample.url, sample.title)}
                    >
                      add
                    </button>
                  </div>
                  <div className="result-meta">{sample.description}</div>
                  <div className="result-meta mono">
                    {sample.licence}
                    {sample.size_mb ? ` · ~${sample.size_mb} MB` : ''}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  )
}
