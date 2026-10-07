/**
 * Explanatory text, kept out of the layout until it is wanted.
 *
 * These notes used to sit under every control as paragraphs, which meant the
 * panel read as documentation with some controls in it. The explanation is
 * still worth having — most of it is the reason a number means what it says —
 * so it moved onto a mark you hover.
 *
 * Warnings do NOT live here. Something that changes what the operator should do
 * has to be visible without being asked for; a warning you must hover to
 * discover is a warning nobody reads. Those stay on the page as text.
 */
export function Info({
  text,
  tone = 'info',
}: {
  text: string
  tone?: 'info' | 'warn'
}) {
  return (
    <span
      className={`hint hint-${tone}`}
      // The native tooltip is doing the work: it survives the sidebar's scroll
      // container, which a positioned bubble would be clipped by, and it needs
      // no positioning code to go wrong at the edges.
      title={text}
      tabIndex={0}
      role="note"
      aria-label={text}
    >
      {tone === 'warn' ? '!' : 'i'}
    </span>
  )
}
