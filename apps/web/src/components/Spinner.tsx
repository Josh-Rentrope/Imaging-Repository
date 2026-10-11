/**
 * A turning ring for work with nothing to measure.
 *
 * Distinct from `ProgressBar` on purpose, and the distinction is the point: a
 * bar says how far, a spinner says only that something is happening. Loading a
 * volume is the first kind of work and has no total to divide by, so a bar there
 * would be a percentage nobody measured.
 */
export function Spinner({ label, className }: { label?: string; className?: string }) {
  return (
    <span
      className={`spinner${className ? ` ${className}` : ''}`}
      role="status"
      aria-live="polite"
    >
      <svg className="spinner__ring" viewBox="0 0 24 24" aria-hidden="true">
        <circle className="spinner__track" cx="12" cy="12" r="9" />
        <path className="spinner__head" d="M21 12a9 9 0 0 0-9-9" />
      </svg>
      {label && <span className="spinner__label">{label}</span>}
    </span>
  )
}
