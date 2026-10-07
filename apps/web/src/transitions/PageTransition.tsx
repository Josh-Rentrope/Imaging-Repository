/**
 * The route transition boundary.
 *
 * How it works, and why it is shaped this way:
 *
 *   - AnimatePresence keys on `location.pathname`, so a route change unmounts one
 *     slot and mounts another. React itself would just swap the DOM instantly;
 *     AnimatePresence is what makes the outgoing slot wait for its exit animation.
 *   - The outgoing slot is absolutely positioned. AnimatePresence runs exit and
 *     enter concurrently, so without this the departing page would sit in normal
 *     flow and shove the arriving one down the screen for the duration of the
 *     animation. With it, container height always comes from the arriving page.
 *   - `initial={false}` suppresses the animation on first paint. Animating the
 *     very first route in reads as a loading flicker rather than a transition.
 *
 * Layout is preserved because the slot is a plain block; only the *outgoing* one
 * is taken out of flow, and only while it is leaving.
 */

import { AnimatePresence, motion, useIsPresent, useReducedMotion } from 'motion/react'
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { useLocation, type Location } from 'react-router'

import {
  TRANSITIONS,
  TRANSITIONS_ENABLED,
  resolvePreset,
  type TransitionPreset,
  type TransitionSpec,
} from './presets'

interface TransitionContextValue {
  /** Explicit override, or null to follow the route map. */
  override: TransitionPreset | null
  setOverride: (preset: TransitionPreset | null) => void
  /** What will actually be used for the current route. */
  effective: TransitionPreset
  routePreset: TransitionPreset
}

const TransitionContext = createContext<TransitionContextValue | null>(null)

export function TransitionProvider({ children }: { children: ReactNode }) {
  const location = useLocation()
  const [override, setOverride] = useState<TransitionPreset | null>(null)

  const routePreset = resolvePreset(location.pathname)
  const effective = override ?? routePreset

  const setOverrideStable = useCallback((preset: TransitionPreset | null) => {
    setOverride(preset)
  }, [])

  const value = useMemo<TransitionContextValue>(
    () => ({ override, setOverride: setOverrideStable, effective, routePreset }),
    [override, setOverrideStable, effective, routePreset],
  )

  return <TransitionContext.Provider value={value}>{children}</TransitionContext.Provider>
}

export function useTransitionControl(): TransitionContextValue {
  const ctx = useContext(TransitionContext)
  if (!ctx) {
    throw new Error('useTransitionControl must be used inside <TransitionProvider>')
  }
  return ctx
}

function PageSlot({
  spec,
  render,
}: {
  spec: TransitionSpec
  render: (location: Location) => ReactNode
}) {
  const isPresent = useIsPresent()
  const current = useLocation()

  // Freeze the location at mount. This is the fix for the classic
  // AnimatePresence-routing bug: while a slot is animating OUT, `useLocation()`
  // already returns the NEW location, so re-rendering it would swap its contents
  // to the incoming route mid-exit and you would see the new page leave. A slot
  // must render the location it was mounted with, for its whole life.
  const [frozen] = useState(current)

  return (
    <motion.div
      className="page-slot"
      variants={spec.variants}
      initial="initial"
      animate="animate"
      exit="exit"
      // Only the departing slot leaves the flow. `pointerEvents: none` stops a
      // fading page from swallowing clicks aimed at the arriving one.
      style={
        isPresent
          ? undefined
          : { position: 'absolute', inset: 0, pointerEvents: 'none', zIndex: 0 }
      }
      aria-hidden={!isPresent}
    >
      {render(frozen)}
    </motion.div>
  )
}

export function PageTransition({ children }: { children: (location: Location) => ReactNode }) {
  const location = useLocation()
  const prefersReducedMotion = useReducedMotion()
  const { effective } = useTransitionControl()

  // Render without the AnimatePresence boundary entirely when disabled, so no
  // absolute positioning or motion work happens at all.
  if (!TRANSITIONS_ENABLED) {
    return <div className="page-stage">{children(location)}</div>
  }

  // Accessibility wins over the route map, and over any manual override.
  const preset: TransitionPreset = prefersReducedMotion ? 'none' : effective
  const spec = TRANSITIONS[preset]

  return (
    <div className="page-stage" data-transition={preset}>
      <AnimatePresence initial={false} mode="sync">
        <PageSlot key={location.pathname} spec={spec} render={children} />
      </AnimatePresence>
    </div>
  )
}
