import { useEffect, useRef, useState, type ReactNode } from 'react'

import {
  anyPlaneEnabled,
  AXIS_COLORS,
  BACKGROUNDS,
  DENSITY_PRESETS,
  fractionToValue,
  OPACITY_UNIT_MAX,
  OPACITY_UNIT_MIN,
  valueToFraction,
  type Axis,
  type DensityPreset,
  type DensityUnit,
  type ViewportSettings,
} from './viewportSettings'

/** A preset's window as fractions, or null when it needs a volume to convert against. */
function presetFractions(
  preset: DensityPreset,
  range: [number, number] | null,
): [number, number] | null {
  if (preset.mode === 'fraction') return [preset.low, preset.high]
  if (!range) return null
  const low = valueToFraction(preset.low, range)
  const high = valueToFraction(preset.high, range)
  return high > low ? [low, high] : null
}

const AXES: Axis[] = ['x', 'y', 'z']

function Popover({
  label,
  active,
  children,
}: {
  label: string
  active?: boolean
  children: (close: () => void) => ReactNode
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onPointerDown = (event: PointerEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) setOpen(false)
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('pointerdown', onPointerDown)
    window.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      window.removeEventListener('keydown', onKey)
    }
  }, [open])

  return (
    <div className="popover" ref={ref}>
      <button
        className={active ? 'tag tag-ok' : 'tag'}
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
      >
        {label}
        {active ? ' •' : ''}
      </button>
      {open && <div className="popover-panel">{children(() => setOpen(false))}</div>}
    </div>
  )
}

function Slider({
  label,
  value,
  min = 0,
  max = 1,
  step = 0.01,
  onChange,
  suffix,
}: {
  label: ReactNode
  value: number
  min?: number
  max?: number
  step?: number
  onChange: (value: number) => void
  suffix?: string
}) {
  return (
    <label className="slider-row">
      <span className="slider-label">{label}</span>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
      />
      <span className="slider-value mono">{suffix ?? value.toFixed(2)}</span>
    </label>
  )
}

/**
 * A density threshold, shown either as a fraction of the range or as the raw
 * value. Stored as a fraction either way so it survives switching sources.
 */
function DensityRow({
  label,
  fraction,
  range,
  unit,
  onChange,
}: {
  label: string
  fraction: number
  range: [number, number] | null
  unit: DensityUnit
  onChange: (fraction: number) => void
}) {
  if (unit === 'value' && range) {
    const span = range[1] - range[0] || 1
    const step = span / 200
    const value = fractionToValue(fraction, range)
    return (
      <label className="slider-row">
        <span className="slider-label">{label}</span>
        <input
          type="range"
          min={range[0]}
          max={range[1]}
          step={step}
          value={value}
          onChange={(event) => onChange(valueToFraction(Number(event.target.value), range))}
        />
        <input
          className="number-input mono"
          type="number"
          min={range[0]}
          max={range[1]}
          step={step}
          value={Math.round(value)}
          onChange={(event) => onChange(valueToFraction(Number(event.target.value), range))}
        />
      </label>
    )
  }

  return (
    <Slider
      label={label}
      value={fraction}
      onChange={onChange}
      suffix={`${Math.round(fraction * 100)}%`}
    />
  )
}

export function ViewportControls({
  settings,
  onChange,
  valueRange,
}: {
  settings: ViewportSettings
  onChange: (next: ViewportSettings) => void
  valueRange: [number, number] | null
}) {
  const patch = (partial: Partial<ViewportSettings>) => onChange({ ...settings, ...partial })

  const setPlane = (axis: Axis, partial: Partial<ViewportSettings['planes'][Axis]>) =>
    patch({ planes: { ...settings.planes, [axis]: { ...settings.planes[axis], ...partial } } })

  const [low, high] = settings.density
  const unit: DensityUnit = valueRange ? settings.densityUnit : 'fraction'
  const rangeLabel = valueRange
    ? `${Math.round(valueRange[0])} … ${Math.round(valueRange[1])}`
    : 'no volume'

  return (
    <div className="viewport-controls">
      <Popover label="Planes" active={anyPlaneEnabled(settings)}>
        {() => (
          <>
            <p className="popover-hint">
              A plane clips away everything on one side. Off by default so the canvas stays free
              for orbiting.
            </p>
            {AXES.map((axis) => {
              const plane = settings.planes[axis]
              return (
                <div key={axis} className="plane-block">
                  <label className="field">
                    <input
                      type="checkbox"
                      checked={plane.enabled}
                      onChange={(event) => setPlane(axis, { enabled: event.target.checked })}
                    />
                    <span
                      className="axis-dot"
                      style={{
                        background: `rgb(${AXIS_COLORS[axis].map((c) => Math.round(c * 255)).join(',')})`,
                      }}
                    />
                    <span className="field-label mono">{axis.toUpperCase()}</span>
                    <button
                      className="ghost"
                      title={plane.showPlane ? 'Hide the plane graphic (the cut stays on)' : 'Show the plane graphic'}
                      disabled={!plane.enabled}
                      onClick={() => setPlane(axis, { showPlane: !plane.showPlane })}
                    >
                      {plane.showPlane ? '👁' : '🚫'}
                    </button>
                    <button
                      className="ghost"
                      title="Cut from the other side"
                      disabled={!plane.enabled}
                      onClick={() => setPlane(axis, { flip: !plane.flip })}
                    >
                      {plane.flip ? '◀' : '▶'}
                    </button>
                  </label>
                  {plane.enabled && (
                    <Slider
                      label="position"
                      value={plane.position}
                      onChange={(value) => setPlane(axis, { position: value })}
                      suffix={`${Math.round(plane.position * 100)}%`}
                    />
                  )}
                </div>
              )
            })}
            <div className="controls">
              <button
                onClick={() =>
                  patch({
                    planes: {
                      x: { enabled: false, showPlane: true, position: 0.5, flip: false },
                      y: { enabled: false, showPlane: true, position: 0.5, flip: false },
                      z: { enabled: false, showPlane: true, position: 0.5, flip: false },
                    },
                  })
                }
              >
                reset
              </button>
            </div>
          </>
        )}
      </Popover>

      <Popover label="Density">
        {() => (
          <>
            <div className="controls" style={{ marginBottom: 8 }}>
              <button
                className={unit === 'fraction' ? 'primary' : ''}
                onClick={() => patch({ densityUnit: 'fraction' })}
              >
                fraction
              </button>
              <button
                className={unit === 'value' ? 'primary' : ''}
                disabled={!valueRange}
                title={valueRange ? 'Raw values from the volume' : 'No volume loaded'}
                onClick={() => patch({ densityUnit: 'value' })}
              >
                value
              </button>
              <span className="muted small mono">{rangeLabel}</span>
            </div>

            <p className="popover-hint">
              Values below the low threshold are dropped. On a CT the raw values are Hounsfield
              units, so bone sits around 300 and above.
            </p>

            <div className="controls" style={{ marginBottom: 8 }}>
              {DENSITY_PRESETS.map((preset) => {
                const target = presetFractions(preset, valueRange)
                const active =
                  target !== null &&
                  Math.abs(low - target[0]) < 0.005 &&
                  Math.abs(high - target[1]) < 0.005
                return (
                  <button
                    key={preset.label}
                    className={active ? 'primary' : ''}
                    title={preset.hint}
                    disabled={target === null}
                    onClick={() => target && patch({ density: target })}
                  >
                    {preset.label}
                  </button>
                )
              })}
            </div>

            <DensityRow
              label="low"
              fraction={low}
              range={valueRange}
              unit={unit}
              onChange={(value) => patch({ density: [Math.min(value, high), high] })}
            />
            <DensityRow
              label="high"
              fraction={high}
              range={valueRange}
              unit={unit}
              onChange={(value) => patch({ density: [low, Math.max(value, low)] })}
            />

            <label className="field" style={{ marginTop: 6 }}>
              <input
                type="checkbox"
                checked={settings.cutAboveHigh}
                onChange={(event) => patch({ cutAboveHigh: event.target.checked })}
              />
              <span className="field-label">cut above high</span>
            </label>
            <p className="popover-hint" style={{ margin: '0 0 4px' }}>
              Off, everything denser than the high value keeps peak opacity — enamel, metal and
              restorations then sit in front of the bone.
            </p>

            <div className="plane-block" style={{ marginTop: 10, paddingTop: 8, borderTop: '1px solid var(--border)' }}>
              <Slider
                label="unit"
                value={settings.opacityUnitDistance}
                min={OPACITY_UNIT_MIN}
                max={OPACITY_UNIT_MAX}
                step={0.05}
                onChange={(value) => patch({ opacityUnitDistance: value })}
                suffix={`${settings.opacityUnitDistance.toFixed(2)}mm`}
              />
              <p className="popover-hint" style={{ margin: '4px 0 0' }}>
                Distance over which opacity accumulates. Smaller makes the volume look solid,
                larger thins it out. This is usually the fastest fix for “everything is opaque”.
              </p>
            </div>

            <div className="controls">
              <button onClick={() => patch({ density: [0, 1], opacityUnitDistance: 1 })}>
                reset
              </button>
            </div>
          </>
        )}
      </Popover>

      <Popover label="Display">
        {(close) => (
          <>
            <div className="swatches">
              {BACKGROUNDS.map((entry) => (
                <button
                  key={entry.name}
                  className={`swatch${settings.background === entry.name ? ' swatch-active' : ''}`}
                  style={{ background: entry.hex }}
                  title={entry.label}
                  onClick={() => {
                    patch({ background: entry.name })
                    close()
                  }}
                />
              ))}
            </div>
            <label className="slider-row">
              <span className="slider-label">custom</span>
              <input
                type="color"
                value={settings.customBackground}
                onChange={(event) =>
                  patch({ background: 'custom', customBackground: event.target.value })
                }
              />
              <span className="slider-value mono">{settings.customBackground}</span>
            </label>
            <label className="field">
              <input
                type="checkbox"
                checked={settings.showVolume}
                onChange={(event) => patch({ showVolume: event.target.checked })}
              />
              <span className="field-label">CT volume</span>
            </label>
            <label className="field">
              <input
                type="checkbox"
                checked={settings.showOrientation}
                onChange={(event) => patch({ showOrientation: event.target.checked })}
              />
              <span className="field-label">orientation axes</span>
            </label>
          </>
        )}
      </Popover>
    </div>
  )
}
