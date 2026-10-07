import { useEffect, useRef, useState, type ReactNode } from 'react'

import type { Job, SourceKind } from '../lib/types'

/**
 * How a layer was made, on hover.
 *
 * Floats over the panel rather than expanding a section into it. The inline
 * version pushed every row below it down, so reading one layer's provenance
 * moved the other layers — and the pointer with them.
 *
 * `position: fixed` rather than absolute: the sidebar scrolls, and an absolutely
 * positioned child is clipped by that scroll container, so a popup near the
 * bottom would be cut off. Fixed escapes the clip because no ancestor
 * establishes a containing block.
 */
const CLOSE_DELAY_MS = 140

/** A step's settings, as label/value pairs. */
function settingsOf(job: Job, op: string): [string, string][] {
  const stage = (job.result?.provenance?.stages ?? []).find((entry) => entry.op === op)
  const params = stage?.params ?? {}
  const rows: [string, string][] = []

  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === '') continue
    // Labels are the point of the panel, so they get names rather than the ids
    // the wire uses — and a list of nine ids is unreadable either way.
    if (Array.isArray(value)) {
      rows.push([key, value.length ? value.join(', ') : 'all'])
    } else if (typeof value === 'boolean') {
      rows.push([key, value ? 'yes' : 'no'])
    } else {
      rows.push([key, String(value)])
    }
  }
  return rows
}

/** What the segmenter actually found, which is not in the params. */
function foundLabels(job: Job): string | null {
  const classes = job.result?.segmentation?.classes ?? []
  if (classes.length === 0) return null
  const names = classes.map((entry) => entry.name)
  const shown = names.slice(0, 8).join(', ')
  return names.length > 8 ? `${shown} +${names.length - 8} more` : shown
}

function PopupBody({
  job,
  sourceKind,
  workflow,
}: {
  job: Job
  sourceKind: SourceKind | null
  workflow: string | null
}) {
  const stages = job.result?.provenance?.stages ?? []
  const scale = job.result?.scale

  return (
    <>
      <div className="prov-line">
        <span className="prov-key">Source</span>
        <span className="mono">{sourceKind === 'dicom' ? 'DICOM' : 'photographs'}</span>
      </div>
      {workflow && (
        <div className="prov-line">
          <span className="prov-key">Workflow</span>
          <span className="mono">{workflow}</span>
        </div>
      )}
      <div className="prov-line">
        <span className="prov-key">Scale</span>
        <span className="mono">
          {scale?.verified ? `verified, ${scale.source}` : 'unverified'}
        </span>
      </div>

      {stages.length > 0 && (
        <>
          <div className="prov-steps-title">Pipeline steps</div>
          {stages.map((stage, index) => {
            const settings = settingsOf(job, stage.op)
            const found = stage.op === 'segment' ? foundLabels(job) : null
            return (
              <div key={`${stage.op}-${index}`} className="prov-step">
                <div className="prov-step-head">
                  <span className="mono">{stage.op}</span>
                  {stage.tool && stage.tool !== '—' && (
                    <span className="prov-tool">{stage.tool}</span>
                  )}
                </div>
                {stage.description && (
                  <div className="prov-desc">{stage.description}</div>
                )}
                {settings.length > 0 && (
                  <ul className="prov-settings">
                    {settings.map(([key, value]) => (
                      <li key={key}>
                        <span className="prov-key">{key}</span>
                        <span className="mono">{value}</span>
                      </li>
                    ))}
                  </ul>
                )}
                {found && (
                  <div className="prov-found">
                    <span className="prov-key">labels</span>
                    <span className="mono">{found}</span>
                  </div>
                )}
              </div>
            )
          })}
        </>
      )}
    </>
  )
}

/**
 * Wraps a trigger and shows the popup beside it while the pointer is on either.
 *
 * The delay is the point: moving from the text to the popup crosses a few pixels
 * that belong to neither, and closing on the first `mouseleave` would make the
 * popup impossible to reach — which is what it used to do.
 */
export function ProvenanceHover({
  job,
  sourceKind,
  workflow,
  children,
}: {
  job: Job
  sourceKind: SourceKind | null
  workflow: string | null
  children: ReactNode
}) {
  const [anchor, setAnchor] = useState<DOMRect | null>(null)
  const timer = useRef<number | null>(null)

  const cancel = () => {
    if (timer.current !== null) {
      window.clearTimeout(timer.current)
      timer.current = null
    }
  }
  const open = (element: HTMLElement) => {
    cancel()
    setAnchor(element.getBoundingClientRect())
  }
  const schedule = () => {
    cancel()
    timer.current = window.setTimeout(() => setAnchor(null), CLOSE_DELAY_MS)
  }

  useEffect(() => cancel, [])

  // A popup anchored to a row that has scrolled away, or to a window that has
  // resized, is worse than none.
  useEffect(() => {
    if (!anchor) return
    const close = () => setAnchor(null)
    globalThis.addEventListener('scroll', close, true)
    globalThis.addEventListener('resize', close)
    return () => {
      globalThis.removeEventListener('scroll', close, true)
      globalThis.removeEventListener('resize', close)
    }
  }, [anchor])

  const width = 316
  // Flipped left of the pointer when there is no room on the right, so the panel
  // never runs off the edge of the window.
  const left = anchor
    ? Math.max(8, Math.min(anchor.left, globalThis.innerWidth - width - 8))
    : 0

  return (
    <>
      <span
        onMouseEnter={(event) => open(event.currentTarget)}
        onMouseLeave={schedule}
        onFocus={(event) => open(event.currentTarget)}
        onBlur={schedule}
      >
        {children}
      </span>

      {anchor && (
        <div
          className="prov-popup"
          style={{ left, top: anchor.bottom + 6, width }}
          onMouseEnter={cancel}
          onMouseLeave={schedule}
        >
          <PopupBody job={job} sourceKind={sourceKind} workflow={workflow} />
        </div>
      )}
    </>
  )
}
