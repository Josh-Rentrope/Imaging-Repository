/**
 * Viewport display settings.
 *
 * Plane positions are stored as fractions of the volume's extent and density
 * thresholds as fractions of its value range, so both survive a switch between
 * sources with different geometry or value ranges instead of silently clipping
 * everything away. `densityUnit` only changes how those fractions are *displayed
 * and entered* — the stored value stays a fraction.
 */

export type Axis = 'x' | 'y' | 'z'

export type BackgroundName = 'dark' | 'mid' | 'light' | 'custom'

/** Fractions are data-independent; values are whatever the volume measures in. */
export type DensityUnit = 'fraction' | 'value'

export interface PlaneSettings {
  enabled: boolean
  /** 0..1 along this axis's extent. */
  position: number
  /** Cut from the far side instead of the near side. */
  flip: boolean
}

export interface ViewportSettings {
  background: BackgroundName
  customBackground: string
  showOrientation: boolean
  /** [low, high] as fractions of the value range. */
  density: [number, number]
  densityUnit: DensityUnit
  /**
   * Make the density window a band-pass: values above `high` go to zero opacity
   * as well as below `low`. Off, the top of the range stays at peak opacity,
   * which lets enamel, restorations and metal artefacts sit in front of bone.
   */
  cutAboveHigh: boolean
  /**
   * Distance over which the opacity curve is applied, in the volume's units
   * (millimetres for DICOM, since spacing comes from PixelSpacing and
   * SliceThickness).
   *
   * Smaller means opacity accumulates faster and the volume looks solid;
   * larger means it thins out. VTK's own default is 1.0.
   */
  opacityUnitDistance: number
  planes: Record<Axis, PlaneSettings>
}

/**
 * Plane tint per axis. Matches the orientation axes, with vtk's default yellow
 * on Y replaced by blue — yellow reads poorly against a light background.
 */
export const AXIS_COLORS: Record<Axis, [number, number, number]> = {
  x: [0.9, 0.25, 0.25],
  y: [0.3, 0.5, 0.95],
  z: [0.25, 0.75, 0.4],
}

export const BACKGROUNDS: { name: BackgroundName; label: string; hex: string }[] = [
  { name: 'dark', label: 'Dark', hex: '#0d0f11' },
  { name: 'mid', label: 'Mid', hex: '#4a5058' },
  { name: 'light', label: 'Light', hex: '#eef1f4' },
]

export const OPACITY_UNIT_MIN = 0.05
export const OPACITY_UNIT_MAX = 5

export const DEFAULT_SETTINGS: ViewportSettings = {
  background: 'dark',
  customBackground: '#7a4a9c',
  showOrientation: true,
  density: [0, 1],
  densityUnit: 'fraction',
  cutAboveHigh: true,
  opacityUnitDistance: 1,
  planes: {
    x: { enabled: false, position: 0.5, flip: false },
    y: { enabled: false, position: 0.5, flip: false },
    z: { enabled: false, position: 0.5, flip: false },
  },
}

export interface DensityPreset {
  label: string
  hint: string
  /**
   * `fraction` is data-independent and always applicable. `value` is in the
   * volume's own units — Hounsfield for a CT — and needs a loaded volume to
   * convert against.
   */
  mode: 'fraction' | 'value'
  low: number
  high: number
}

/**
 * A CT window, because that is what these are usually pointed at once rescale
 * has been applied. The bone window came out of tuning against a real CBCT:
 * below ~80 is soft tissue, above ~1000 is enamel, restorations and metal, and
 * with `cutAboveHigh` on those stop being drawn over the bone.
 */
export const DENSITY_PRESETS: DensityPreset[] = [
  { label: 'All', hint: 'Full range, air included', mode: 'fraction', low: 0, high: 1 },
  { label: 'Soft', hint: 'Soft tissue and up — −200 … 500 HU', mode: 'value', low: -200, high: 500 },
  { label: 'Bone', hint: 'Cortical bone — 80 … 1000 HU', mode: 'value', low: 80, high: 1000 },
  { label: 'Dense', hint: 'Enamel, metal, artefacts — 1000 … 3000 HU', mode: 'value', low: 1000, high: 3000 },
]

export function backgroundHex(settings: ViewportSettings): string {
  if (settings.background === 'custom') return settings.customBackground
  return BACKGROUNDS.find((b) => b.name === settings.background)?.hex ?? '#0d0f11'
}

export function hexToRgb01(hex: string): [number, number, number] {
  const match = /^#?([0-9a-f]{6})$/i.exec(hex.trim())
  if (!match) return [0.05, 0.06, 0.07]
  const value = parseInt(match[1], 16)
  return [((value >> 16) & 255) / 255, ((value >> 8) & 255) / 255, (value & 255) / 255]
}

export function anyPlaneEnabled(settings: ViewportSettings): boolean {
  return (['x', 'y', 'z'] as Axis[]).some((axis) => settings.planes[axis].enabled)
}

export function fractionToValue(fraction: number, range: [number, number]): number {
  return range[0] + (range[1] - range[0]) * fraction
}

export function valueToFraction(value: number, range: [number, number]): number {
  const span = range[1] - range[0]
  if (!span) return 0
  return Math.min(1, Math.max(0, (value - range[0]) / span))
}
