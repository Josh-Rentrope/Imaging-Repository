/**
 * Named transition presets.
 *
 * Built now rather than later on purpose: page transitions are cheap to add when
 * the router is already wrapped in an AnimatePresence boundary and expensive
 * afterwards, because every route component has to be re-parented.
 *
 * Every preset runs the outgoing and incoming pages concurrently. The outgoing
 * layer is absolutely positioned by the boundary (see PageTransition.tsx), so it
 * cannot push the incoming page around and the container height always comes
 * from the page that is actually arriving. That single mechanism gives correct
 * crossfades and correct wipes; mixing AnimatePresence `wait`/`sync` modes per
 * route does not, because the mode is a property of the boundary, not the route.
 *
 * `clipPath` is used for wipes rather than width/translate because it is
 * compositor-friendly and doesn't trigger layout on every frame.
 *
 * Timing: 180-320ms. Long enough to read as intentional, short enough that a
 * clinician clicking between patients never waits on an animation. Anything
 * above ~400ms starts to feel like the app is slow rather than smooth.
 */

import type { Variants } from 'motion/react'

/**
 * Master switch. Route transitions are off while the editor layout is being
 * settled; flip to true to re-enable the route map below.
 */
export const TRANSITIONS_ENABLED = false

export type TransitionPreset =
  | 'none'
  | 'fade'
  | 'dissolve'
  | 'wipeLeft'
  | 'wipeRight'
  | 'wipeUp'
  | 'wipeDown'
  | 'rise'

export interface TransitionSpec {
  variants: Variants
  /** Used for the AnimatePresence boundary and to debounce rapid nav. */
  durationMs: number
  /** One-line description, surfaced in the transitions playground. */
  description: string
}

const EASE_OUT = [0.22, 1, 0.36, 1] as const
const EASE_IN = [0.64, 0, 0.78, 0] as const
const EASE_IN_OUT = [0.65, 0, 0.35, 1] as const

/**
 * A wipe that reveals the incoming page from one edge while the outgoing page
 * retreats the same way. `from` names the edge the incoming page sweeps in from.
 */
function wipe(from: 'left' | 'right' | 'top' | 'bottom', durationMs: number): TransitionSpec {
  const hidden = {
    left: 'inset(0 100% 0 0)',
    right: 'inset(0 0 0 100%)',
    top: 'inset(0 0 100% 0)',
    bottom: 'inset(100% 0 0 0)',
  }[from]

  return {
    durationMs,
    description: `Incoming page sweeps in from the ${from}.`,
    variants: {
      initial: { clipPath: hidden },
      animate: {
        clipPath: 'inset(0 0 0 0)',
        transition: { duration: durationMs / 1000, ease: EASE_OUT },
      },
      exit: {
        // Retracts the same way it came, so the pair reads as one sweep.
        clipPath: hidden,
        transition: { duration: durationMs / 1000, ease: EASE_IN },
      },
    },
  }
}

export const TRANSITIONS: Record<TransitionPreset, TransitionSpec> = {
  none: {
    durationMs: 0,
    description: 'Instant. Used when prefers-reduced-motion is set.',
    variants: {
      initial: { opacity: 1 },
      animate: { opacity: 1 },
      exit: { opacity: 1 },
    },
  },

  fade: {
    durationMs: 200,
    description: 'Straight crossfade. The default between sibling views.',
    variants: {
      initial: { opacity: 0 },
      animate: { opacity: 1, transition: { duration: 0.2, ease: EASE_OUT } },
      exit: { opacity: 0, transition: { duration: 0.2, ease: EASE_IN } },
    },
  },

  dissolve: {
    durationMs: 300,
    description: 'Fade with a touch of scale and blur. Softer, more deliberate.',
    variants: {
      initial: { opacity: 0, scale: 1.012, filter: 'blur(6px)' },
      animate: {
        opacity: 1,
        scale: 1,
        filter: 'blur(0px)',
        transition: { duration: 0.3, ease: EASE_OUT },
      },
      exit: {
        opacity: 0,
        scale: 0.994,
        filter: 'blur(6px)',
        transition: { duration: 0.24, ease: EASE_IN },
      },
    },
  },

  wipeLeft: wipe('right', 320),
  wipeRight: wipe('left', 320),
  wipeUp: wipe('bottom', 320),
  wipeDown: wipe('top', 320),

  rise: {
    durationMs: 280,
    description: 'Content lifts into place. Good for drill-in navigation.',
    variants: {
      initial: { opacity: 0, y: 18 },
      animate: { opacity: 1, y: 0, transition: { duration: 0.28, ease: EASE_IN_OUT } },
      exit: { opacity: 0, y: -12, transition: { duration: 0.2, ease: EASE_IN } },
    },
  },
}

export const TRANSITION_PRESETS = Object.keys(TRANSITIONS) as TransitionPreset[]

/** Route path -> preset. Longest matching prefix wins; see resolvePreset(). */
export const ROUTE_TRANSITIONS: Record<string, TransitionPreset> = {
  '/': 'dissolve',
  '/reconstruct': 'wipeUp',
  '/dicom': 'wipeLeft',
  '/jobs': 'rise',
  '/transitions': 'fade',
}

export function resolvePreset(pathname: string): TransitionPreset {
  if (pathname in ROUTE_TRANSITIONS) return ROUTE_TRANSITIONS[pathname]

  const matches = Object.keys(ROUTE_TRANSITIONS)
    .filter((route) => route !== '/' && pathname.startsWith(route))
    .sort((a, b) => b.length - a.length)

  return matches.length > 0 ? (ROUTE_TRANSITIONS[matches[0]] as TransitionPreset) : 'dissolve'
}
