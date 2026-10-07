import { useCallback, useRef, useState, type DragEvent } from 'react'

/**
 * Drop target for DICOM series and image sets.
 *
 * Handles dropped directories: a CBCT export is a folder of a few hundred
 * slices, and dragging the folder is the natural gesture. `webkitGetAsEntry`
 * is the only way the browser exposes a directory's contents.
 */

const DICOM_EXTENSIONS = ['.dcm', '.dicom', '.ima']
const IMAGE_EXTENSIONS = ['.jpg', '.jpeg', '.png', '.heic', '.tif', '.tiff', '.webp']

export function classify(files: File[]): 'dicom' | 'images' | 'mixed' {
  let dicom = 0
  let image = 0
  for (const file of files) {
    const name = file.name.toLowerCase()
    if (DICOM_EXTENSIONS.some((ext) => name.endsWith(ext)) || file.type === 'application/dicom') {
      dicom += 1
    } else if (IMAGE_EXTENSIONS.some((ext) => name.endsWith(ext)) || file.type.startsWith('image/')) {
      image += 1
    } else {
      // Extensionless scanner output is treated as DICOM: the parser decides.
      dicom += 1
    }
  }
  if (dicom > 0 && image > 0) return 'mixed'
  return image > 0 ? 'images' : 'dicom'
}

async function readEntry(entry: FileSystemEntry, out: File[]): Promise<void> {
  if (entry.isFile) {
    await new Promise<void>((resolve) => {
      ;(entry as FileSystemFileEntry).file(
        (file) => {
          out.push(file)
          resolve()
        },
        () => resolve(),
      )
    })
    return
  }

  if (entry.isDirectory) {
    const reader = (entry as FileSystemDirectoryEntry).createReader()
    // readEntries returns at most ~100 entries per call and signals completion
    // with an empty batch. One call silently truncates a large series.
    for (;;) {
      const batch = await new Promise<FileSystemEntry[]>((resolve) => {
        reader.readEntries(
          (entries) => resolve(entries),
          () => resolve([]),
        )
      })
      if (batch.length === 0) break
      for (const child of batch) await readEntry(child, out)
    }
  }
}

export function DropZone({
  onFiles,
  disabled,
}: {
  onFiles: (files: File[], kind: 'dicom' | 'images') => void
  disabled?: boolean
}) {
  const [active, setActive] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const depth = useRef(0)

  const handleDrop = useCallback(
    async (event: DragEvent<HTMLDivElement>) => {
      event.preventDefault()
      depth.current = 0
      setActive(false)
      if (disabled) return

      const items = Array.from(event.dataTransfer.items ?? [])
      const entries = items
        .map(
          (item) =>
            (item as DataTransferItem & { webkitGetAsEntry?: () => FileSystemEntry | null })
              .webkitGetAsEntry?.() ?? null,
        )
        .filter((entry): entry is FileSystemEntry => entry !== null)

      let files: File[]
      if (entries.length === 0) {
        files = Array.from(event.dataTransfer.files ?? [])
      } else {
        const collected: File[] = []
        for (const entry of entries) await readEntry(entry, collected)
        files = collected
      }

      if (files.length === 0) return
      files.sort((a, b) => a.name.localeCompare(b.name))
      const kind = classify(files)
      onFiles(files, kind === 'images' ? 'images' : 'dicom')
    },
    [disabled, onFiles],
  )

  return (
    <div
      className="dropzone"
      data-active={active}
      onClick={() => !disabled && inputRef.current?.click()}
      onDragEnter={(event) => {
        event.preventDefault()
        depth.current += 1
        setActive(true)
      }}
      onDragOver={(event) => event.preventDefault()}
      onDragLeave={(event) => {
        event.preventDefault()
        depth.current -= 1
        if (depth.current <= 0) setActive(false)
      }}
      onDrop={handleDrop}
      role="button"
      tabIndex={0}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') inputRef.current?.click()
      }}
    >
      <input
        ref={inputRef}
        type="file"
        multiple
        {...({ webkitdirectory: '' } as Record<string, string>)}
        style={{ display: 'none' }}
        onChange={(event) => {
          const files = Array.from(event.target.files ?? [])
          if (files.length > 0) {
            const kind = classify(files)
            onFiles(files, kind === 'images' ? 'images' : 'dicom')
          }
          event.target.value = ''
        }}
      />
      <div>Drop DICOM or images</div>
      <div className="muted" style={{ fontSize: 11, marginTop: 2 }}>
        or click to browse
      </div>
    </div>
  )
}
