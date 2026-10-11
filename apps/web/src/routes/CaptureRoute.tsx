/**
 * Capture, from a phone.
 *
 * Deliberately outside the editor shell: that shell is a desktop workspace
 * switcher, viewport and result panel, and a phone rendering it spends its
 * screen on chrome and its bandwidth on fetches nobody asked for. It shares the
 * editor's *state* -- the working set a capture lands in is the one the desktop
 * is looking at -- and nothing else.
 *
 * It reaches the server through `api.uploadImages`, the same call the drop zone
 * uses, with the same kind of argument. Nothing here knows what a source is.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router'

import { api } from '../lib/api'
import {
  extractFrames,
  grabFrame,
  openCamera,
  pickRecorderType,
  VIDEO_ATTRIBUTES,
  type CameraHandle,
} from '../lib/capture'
import { useEditor } from '../state/editor'

type Mode = 'photos' | 'camera' | 'video'

const MODES: { id: Mode; label: string; blurb: string }[] = [
  {
    id: 'photos',
    label: 'Photos',
    blurb:
      'Opens the system camera, so it uses the full sensor and the real flash. One shot per visit unless your phone lets you pick several.',
  },
  {
    id: 'camera',
    label: 'In-page camera',
    blurb:
      'Tap repeatedly to take many overlapping frames without leaving the page. Lower resolution: a browser gives the video stream, around two megapixels, not the sensor.',
  },
  {
    id: 'video',
    label: 'Video',
    blurb:
      'Record a slow arc, then take frames from it with the flash on and again with it off. Two passes is the usual way to get around a surface that glares.',
  },
]

export default function CaptureRoute() {
  const { activeWorkspace, activeSet, refreshSources } = useEditor()
  const [mode, setMode] = useState<Mode>('photos')
  const [shots, setShots] = useState<File[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [imported, setImported] = useState<string | null>(null)

  // Monotonic across the whole visit, so a set assembled from more than one mode
  // still sorts into the order it was taken. Ordering is the only thing carrying
  // the sequence -- the server sorts by name and nothing records an order
  // separately.
  const counter = useRef(0)
  const nextName = useCallback(() => {
    counter.current += 1
    return `capture-${String(counter.current).padStart(4, '0')}.jpg`
  }, [])

  const add = useCallback((files: File[]) => {
    setImported(null)
    setShots((current) => [...current, ...files])
  }, [])

  const totalBytes = shots.reduce((sum, file) => sum + file.size, 0)

  const upload = async () => {
    if (!shots.length || busy) return
    setBusy(true)
    setError(null)
    try {
      const source = await api.uploadImages(shots, activeWorkspace.id, activeSet.id)
      await refreshSources()
      setImported(source.name)
      setShots([])
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="capture">
      <header className="capture__bar">
        <Link className="capture__back" to="/">
          Editor
        </Link>
        <div className="capture__where">
          <span className="capture__set">{activeSet.name}</span>
          <span className="capture__workspace">{activeWorkspace.name}</span>
        </div>
      </header>

      <nav className="capture__modes" role="tablist">
        {MODES.map((entry) => (
          <button
            key={entry.id}
            role="tab"
            aria-selected={mode === entry.id}
            className={mode === entry.id ? 'capture__mode is-active' : 'capture__mode'}
            onClick={() => setMode(entry.id)}
          >
            {entry.label}
          </button>
        ))}
      </nav>

      <p className="capture__blurb">{MODES.find((entry) => entry.id === mode)?.blurb}</p>

      <main className="capture__stage">
        {mode === 'photos' && <PhotosCapture onShots={add} />}
        {mode === 'camera' && <CameraCapture onShots={add} nextName={nextName} />}
        {mode === 'video' && <VideoCapture onShots={add} nextName={nextName} />}
      </main>

      {error && <p className="capture__error">{error}</p>}
      {imported && <p className="capture__ok">Uploaded as “{imported}”.</p>}

      <footer className="capture__tray">
        <ShotStrip shots={shots} onClear={() => setShots([])} />
        <div className="capture__actions">
          <span className="capture__count">
            {shots.length ? `${shots.length} · ${megabytes(totalBytes)}` : 'Nothing captured yet'}
          </span>
          <button
            className="capture__upload"
            disabled={!shots.length || busy}
            onClick={() => void upload()}
          >
            {busy ? 'Uploading…' : 'Upload'}
          </button>
        </div>
      </footer>
    </div>
  )
}

function PhotosCapture({ onShots }: { onShots: (files: File[]) => void }) {
  const input = useRef<HTMLInputElement>(null)

  return (
    <div className="capture__panel">
      <button className="capture__shutter" onClick={() => input.current?.click()}>
        Take photos
      </button>
      <input
        ref={input}
        type="file"
        accept="image/*"
        // The system camera where the browser honours it. Safari does not, and
        // gives the normal picker instead -- which still reaches the camera app,
        // so the flash and the sensor are available either way.
        capture="environment"
        multiple
        hidden
        onChange={(event) => {
          const files = Array.from(event.target.files ?? [])
          if (files.length) onShots(files)
          // Reset, so choosing the same photograph twice in a row still fires.
          event.target.value = ''
        }}
      />
      <p className="capture__hint">
        Forty to eighty overlapping frames is a good target. Move around the
        subject rather than turning on the spot.
      </p>
    </div>
  )
}

function CameraCapture({
  onShots,
  nextName,
}: {
  onShots: (files: File[]) => void
  nextName: () => string
}) {
  const video = useRef<HTMLVideoElement>(null)
  const [camera, setCamera] = useState<CameraHandle | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [torch, setTorch] = useState(false)
  const [taken, setTaken] = useState(0)

  useEffect(() => {
    let opened: CameraHandle | null = null
    let cancelled = false

    openCamera()
      .then((handle) => {
        if (cancelled) {
          handle.stop()
          return
        }
        opened = handle
        setCamera(handle)
        // Always rendered, so the ref is there by the time this resolves.
        if (video.current) video.current.srcObject = handle.stream
      })
      .catch((cause: unknown) => {
        setError(cause instanceof Error ? cause.message : String(cause))
      })

    return () => {
      cancelled = true
      opened?.stop()
    }
  }, [])

  const shoot = async () => {
    if (!video.current || !camera) return
    try {
      onShots([await grabFrame(video.current, nextName())])
      setTaken((count) => count + 1)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }

  return (
    <div className="capture__panel">
      <div className="capture__viewfinder">
        <video ref={video} {...VIDEO_ATTRIBUTES} className="capture__video" />
        {!camera && !error && <p className="capture__overlay">Starting the camera…</p>}
      </div>

      {error && <p className="capture__error">{error}</p>}

      <div className="capture__controls">
        <button className="capture__shutter" disabled={!camera} onClick={() => void shoot()}>
          Capture frame
        </button>
        {/* Only where the track admits to one. iOS usually does not, and the
            honest answer there is the system camera in the Photos tab. */}
        {camera?.torch && (
          <button
            className={torch ? 'capture__toggle is-on' : 'capture__toggle'}
            onClick={async () => {
              const next = !torch
              await camera.setTorch(next)
              setTorch(next)
            }}
          >
            {torch ? 'Light on' : 'Light off'}
          </button>
        )}
      </div>
      <p className="capture__hint">{taken} taken this session.</p>
    </div>
  )
}

function VideoCapture({
  onShots,
  nextName,
}: {
  onShots: (files: File[]) => void
  nextName: () => string
}) {
  const video = useRef<HTMLVideoElement>(null)
  const camera = useRef<CameraHandle | null>(null)
  const recorder = useRef<MediaRecorder | null>(null)
  const chunks = useRef<Blob[]>([])
  const [armed, setArmed] = useState(false)
  const [recording, setRecording] = useState(false)
  const [working, setWorking] = useState(false)
  const [wanted, setWanted] = useState(16)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let opened: CameraHandle | null = null
    let cancelled = false

    openCamera()
      .then((handle) => {
        if (cancelled) {
          handle.stop()
          return
        }
        opened = handle
        camera.current = handle
        setArmed(true)
        if (video.current) video.current.srcObject = handle.stream
      })
      .catch((cause: unknown) => {
        setError(cause instanceof Error ? cause.message : String(cause))
      })

    return () => {
      cancelled = true
      if (recorder.current?.state === 'recording') recorder.current.stop()
      opened?.stop()
    }
  }, [])

  const start = () => {
    if (!camera.current) return
    const type = pickRecorderType()
    if (!type) {
      setError('This browser cannot record video.')
      return
    }
    chunks.current = []
    const machine = new MediaRecorder(camera.current.stream, { mimeType: type })
    machine.ondataavailable = (event) => {
      if (event.data.size) chunks.current.push(event.data)
    }
    machine.onstop = () => {
      void harvest(new Blob(chunks.current, { type }))
    }
    recorder.current = machine
    machine.start()
    setError(null)
    setRecording(true)
  }

  const stop = () => {
    recorder.current?.stop()
    setRecording(false)
  }

  const harvest = async (recording: Blob) => {
    setWorking(true)
    try {
      // Frames are taken here, in the browser, rather than uploaded as a video:
      // the server has no video pipeline and would store the file as a source
      // whose every "image" is a webm. Extraction here also means what lands on
      // the server is the same kind of thing a folder of photographs produces.
      onShots(await extractFrames(recording, wanted, nextName))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setWorking(false)
    }
  }

  return (
    <div className="capture__panel">
      <div className="capture__viewfinder">
        <video ref={video} {...VIDEO_ATTRIBUTES} className="capture__video" />
        {!armed && !error && <p className="capture__overlay">Starting the camera…</p>}
        {recording && <span className="capture__recording">Recording</span>}
      </div>

      {error && <p className="capture__error">{error}</p>}

      <div className="capture__controls">
        <button
          className={recording ? 'capture__shutter is-recording' : 'capture__shutter'}
          disabled={!armed || working}
          onClick={recording ? stop : start}
        >
          {recording ? 'Stop' : 'Record'}
        </button>
        <label className="capture__field">
          Frames
          <input
            type="number"
            min={4}
            max={80}
            value={wanted}
            onChange={(event) => setWanted(clamp(Number(event.target.value), 4, 80))}
          />
        </label>
      </div>
      <p className="capture__hint">
        {working ? 'Taking frames…' : 'Record a slow arc, then stop.'}
      </p>
    </div>
  )
}

function ShotStrip({ shots, onClear }: { shots: File[]; onClear: () => void }) {
  const [urls, setUrls] = useState<string[]>([])

  useEffect(() => {
    const created = shots.map((file) => URL.createObjectURL(file))
    setUrls(created)
    // Revoked on change, or a long session leaks every frame it ever took.
    return () => created.forEach((url) => URL.revokeObjectURL(url))
  }, [shots])

  if (!shots.length) return null

  return (
    <div className="capture__strip">
      {urls.slice(-12).map((url, index) => (
        <img key={url} src={url} alt="" className="capture__thumb" data-index={index} />
      ))}
      {shots.length > 12 && <span className="capture__more">+{shots.length - 12}</span>}
      <button className="capture__clear" onClick={onClear}>
        Clear
      </button>
    </div>
  )
}

function megabytes(bytes: number): string {
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function clamp(value: number, low: number, high: number): number {
  if (!Number.isFinite(value)) return low
  return Math.min(high, Math.max(low, Math.round(value)))
}
