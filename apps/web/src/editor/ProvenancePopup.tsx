import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'

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
 *
 * **Fixed positioning escapes the scroll container's clip but not the window's.**
 * The Results section is the lowest thing on the page, so the rows this is
 * anchored to sit near the bottom of the viewport and a popup hung below them
 * ran off the end of the screen — the one place it was most needed was the one
 * place it could not be read. So the placement is measured and chosen: below the
 * anchor when that fits, above it when that fits better, and capped to the room
 * that is actually there, scrolling inside itself if the content is longer.
 */
const CLOSE_DELAY_MS = 140

/** Gap between the anchor and the popup, and the margin kept from the window edge. */
const GAP_PX = 6
const MARGIN_PX = 8

/** Never shrink below this; below it the popup stops being readable. */
const MIN_USEFUL_HEIGHT = 120

/**
 * Where to put the popup, given what it measures and where its row sits.
 *
 * Split out from the component because it is the whole of the bug and none of
 * the DOM: three numbers in, a top and a height out, so it can be checked
 * against the cases that used to clip without standing up a browser.
 */
export function choosePlacement(
  anchor: { top: number; bottom: number },
  natural: number,
  viewport: number,
): { top: number; maxHeight: number } {
  const roomBelow = viewport - (anchor.bottom + GAP_PX) - MARGIN_PX
  const roomAbove = anchor.top - GAP_PX - MARGIN_PX

  // Below is the natural reading position — the pointer is already moving down
  // into it — so it wins ties, and is only given up when the other side
  // genuinely has more room.
  const placeBelow = natural <= roomBelow || roomBelow >= roomAbove
  const room = Math.max(placeBelow ? roomBelow : roomAbove, MIN_USEFUL_HEIGHT)
  const height = Math.min(natural, room)

  return {
    top: placeBelow
      ? anchor.bottom + GAP_PX
      : Math.max(MARGIN_PX, anchor.top - GAP_PX - height),
    maxHeight: room,
  }
}

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
  const warnings = job.result?.warnings ?? []
  const placement = job.result?.geometry?.mask_placement

  return (
    <>
      {/* First, because a result that warned about itself is the one thing here
          that changes what a reader should do with everything below it. */}
      {warnings.length > 0 && (
        <ul className="prov-warnings">
          {warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
      )}

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
      {placement && (
        <div className="prov-line">
          <span className="prov-key">Placement</span>
          <span className={`mono${placement.agrees ? '' : ' warn-text'}`}>
            {placement.overlap_pct.toFixed(1)}% in volume
          </span>
        </div>
      )}

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
  const [box, setBox] = useState<{ top: number; maxHeight: number } | null>(null)
  const timer = useRef<number | null>(null)
  const popupRef = useRef<HTMLDivElement | null>(null)

  const cancel = () => {
    if (timer.current !== null) {
      window.clearTimeout(timer.current)
      timer.current = null
    }
  }
  const open = (element: HTMLElement) => {
    cancel()
    setBox(null)
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
    const close = (event: Event) => {
      // Ignore a scroll that came from inside the popup. This listener is in the
      // capture phase, so it also sees the popup scrolling its own overflow —
      // and closing there made a long popup impossible to read past its first
      // screenful, which is exactly the case the scrollbar exists for.
      if (popupRef.current?.contains(event.target as Node)) return
      setAnchor(null)
    }
    const onResize = () => setAnchor(null)
    globalThis.addEventListener('scroll', close, true)
    globalThis.addEventListener('resize', onResize)
    return () => {
      globalThis.removeEventListener('scroll', close, true)
      globalThis.removeEventListener('resize', onResize)
    }
  }, [anchor])

  /**
   * Choose the vertical placement once the popup has been rendered and measured.
   *
   * `useLayoutEffect` rather than `useEffect`: it runs after the DOM is updated
   * but before the browser paints, so measuring here and re-rendering with the
   * answer happens in the same frame. With `useEffect` the popup would paint
   * once in the wrong place and jump.
   *
   * `scrollHeight` is the full content height even while `max-height` is
   * clamping the element, which is what makes the fit check possible at all —
   * `offsetHeight` would only ever report the clamped box.
   */
  useLayoutEffect(() => {
    const element = popupRef.current
    if (!anchor || !element) return
    setBox(choosePlacement(anchor, element.scrollHeight, globalThis.innerHeight))
  }, [anchor])

  const width = 316
  // Flipped left of the pointer when there is no room on the right, so the panel
  // never runs off the edge of the window.
  const left = anchor
    ? Math.max(MARGIN_PX, Math.min(anchor.left, globalThis.innerWidth - width - MARGIN_PX))
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
          ref={popupRef}
          className="prov-popup"
          style={{
            left,
            width,
            // Rendered before it is positioned so it can be measured, and hidden
            // for that one pre-paint frame rather than flashing at the top-left.
            visibility: box ? 'visible' : 'hidden',
            top: box?.top ?? 0,
            maxHeight: box?.maxHeight ?? MIN_USEFUL_HEIGHT,
          }}
          onMouseEnter={cancel}
          onMouseLeave={schedule}
        >
          <PopupBody job={job} sourceKind={sourceKind} workflow={workflow} />
        </div>
      )}
    </>
  )
}
