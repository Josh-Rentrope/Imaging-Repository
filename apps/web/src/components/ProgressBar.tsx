/**
 * A bar for something that is running.
 *
 * Indeterminate when there is no total to divide by, which is a real state and
 * not a failure: COLMAP's mapper cannot know in advance how many of its images
 * will register. A bar drawn against an unknown total is a percentage nobody
 * measured, so it animates instead of filling.
 */
export function ProgressBar({
  label,
  fraction,
  detail,
}: {
  label: string
  /** 0–1, or null when the step cannot know how far along it is. */
  fraction: number | null
  /** What to put on the right: a count, a stage, a percentage. */
  detail?: string
}) {
  const known = fraction !== null
  return (
    <div
      className="progress"
      role="progressbar"
      aria-label={label}
      aria-valuenow={known ? Math.round(fraction * 100) : undefined}
      aria-valuemin={known ? 0 : undefined}
      aria-valuemax={known ? 100 : undefined}
    >
      <div className="progress__row">
        <span className="progress__label">{label}</span>
        {detail && <span className="progress__detail mono">{detail}</span>}
      </div>
      <div className={`progress__track${known ? '' : ' is-indeterminate'}`}>
        <div
          className="progress__fill"
          style={known ? { width: `${Math.max(1, Math.round(fraction * 100))}%` } : undefined}
        />
      </div>
    </div>
  )
}
