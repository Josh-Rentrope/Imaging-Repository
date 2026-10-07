import { useState, type ReactNode } from 'react'

/**
 * A collapsible section of the side panel.
 *
 * **Only the title toggles.** The header holds two things — the toggle and an
 * `actions` slot — and they are siblings, not nested. Putting the action buttons
 * inside the button would make "None" on Export both clear the selection and
 * collapse the panel it lives in, which is the kind of thing that reads as the
 * app being broken rather than as a stray click. Keeping them out of the button
 * element is what makes that impossible rather than merely unlikely.
 *
 * Open state is remembered per section id. The panel is the working surface and
 * it gets reloaded constantly in development, so re-collapsing the same three
 * sections on every reload is a tax paid for nothing.
 */

const STORAGE_KEY = 'boneviewer.sections'

function readState(): Record<string, boolean> {
  try {
    const raw = globalThis.localStorage?.getItem(STORAGE_KEY)
    const parsed: unknown = raw ? JSON.parse(raw) : null
    return parsed && typeof parsed === 'object' ? (parsed as Record<string, boolean>) : {}
  } catch {
    // Private mode, a full quota, or something else having written nonsense to
    // the key. None of that is a reason for the panel to stop working.
    return {}
  }
}

function writeState(id: string, open: boolean): void {
  try {
    globalThis.localStorage?.setItem(STORAGE_KEY, JSON.stringify({ ...readState(), [id]: open }))
  } catch {
    // As above: remembering is a convenience, not a requirement.
  }
}

export function Section({
  id,
  title,
  count,
  actions,
  defaultOpen = true,
  children,
}: {
  id: string
  title: string
  /** Shown dimmed beside the title. Not interactive, so it can live in the toggle. */
  count?: ReactNode
  /** Buttons for this section. Deliberately outside the toggle. */
  actions?: ReactNode
  defaultOpen?: boolean
  children: ReactNode
}) {
  const [open, setOpen] = useState(() => readState()[id] ?? defaultOpen)

  function toggle() {
    const next = !open
    setOpen(next)
    writeState(id, next)
  }

  return (
    <div className="section">
      <div className="section-head">
        <h2 className="section-title">
          <button
            type="button"
            className="section-toggle"
            aria-expanded={open}
            aria-controls={`section-body-${id}`}
            onClick={toggle}
          >
            {/* A character rather than an SVG: it rotates with CSS, inherits the
                text colour, and needs no asset. */}
            <span className={`section-arrow${open ? ' open' : ''}`} aria-hidden="true">
              ▶
            </span>
            <span className="section-name">{title}</span>
            {count != null && <span className="section-count">{count}</span>}
          </button>
        </h2>
        {actions && <div className="section-actions">{actions}</div>}
      </div>

      {open && (
        <div className="section-body" id={`section-body-${id}`}>
          {children}
        </div>
      )}
    </div>
  )
}
