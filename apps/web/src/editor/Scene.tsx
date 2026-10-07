import { useEffect, useRef, useState } from 'react'

// Side-effect imports: these register the rendering backends. Without them the
// mappers construct but nothing draws.
import '@kitware/vtk.js/Rendering/Profiles/Geometry'
import '@kitware/vtk.js/Rendering/Profiles/Volume'

import vtkDataArray from '@kitware/vtk.js/Common/Core/DataArray'
import vtkPlane from '@kitware/vtk.js/Common/DataModel/Plane'
import vtkPiecewiseFunction from '@kitware/vtk.js/Common/DataModel/PiecewiseFunction'
import vtkImageData from '@kitware/vtk.js/Common/DataModel/ImageData'
import vtkPlaneSource from '@kitware/vtk.js/Filters/Sources/PlaneSource'
import vtkOrientationMarkerWidget from '@kitware/vtk.js/Interaction/Widgets/OrientationMarkerWidget'
import { Corners } from '@kitware/vtk.js/Interaction/Widgets/OrientationMarkerWidget/Constants'
import vtkActor from '@kitware/vtk.js/Rendering/Core/Actor'
import vtkAxesActor from '@kitware/vtk.js/Rendering/Core/AxesActor'
import vtkColorTransferFunction from '@kitware/vtk.js/Rendering/Core/ColorTransferFunction'
import vtkMapper from '@kitware/vtk.js/Rendering/Core/Mapper'
import vtkVolume from '@kitware/vtk.js/Rendering/Core/Volume'
import vtkVolumeMapper from '@kitware/vtk.js/Rendering/Core/VolumeMapper'
import vtkPLYReader from '@kitware/vtk.js/IO/Geometry/PLYReader'
import vtkSTLReader from '@kitware/vtk.js/IO/Geometry/STLReader'
import vtkGenericRenderWindow from '@kitware/vtk.js/Rendering/Misc/GenericRenderWindow'

import { artifactUrl, fetchArtifact } from '../lib/api'
import type { VolumeHeader } from '../lib/types'
import { intersectBox, intersectMesh, type Ray, type Visibility } from './raycast'
import {
  AXIS_COLORS,
  backgroundHex,
  hexToRgb01,
  type Axis,
  type ViewportSettings,
} from './viewportSettings'

export interface SceneVolume {
  headerRef: string
  binRef: string
}

/** One pipeline result being displayed, with its own display flags. */
export interface SceneSurface {
  id: string
  meshRef: string
  format: string
  visible: boolean
  /** Whether the cutting planes apply to this surface. */
  clipped: boolean
  color: [number, number, number]
  /** Per-vertex label sidecar, for a surface that came out of a segmentation. */
  labelsRef: string | null
  labelsHeaderRef: string | null
}

export interface SceneModel {
  volume: SceneVolume | null
  surfaces: SceneSurface[]
}

/** What a click on labelled geometry resolved to. */
export interface SceneSelection {
  surfaceId: string
  label: number
  /** Human name from the segmentation legend, or a stand-in when it has none. */
  name: string
  /** Vertices carrying this label, so the popup can say how much was picked. */
  vertices: number
  /** Where the click landed, in CSS pixels from the viewport's top-left. */
  x: number
  y: number
}

interface VertexLabels {
  values: Int32Array
  legend: Record<string, string>
  counts: Record<string, number>
}

// `VolumeHeader` is imported rather than redeclared. It used to live here as a
// second copy, which is how this file kept rendering without `direction` while
// the shared type had it: the duplicate silently disagreed with the one the
// rest of the app uses, and TypeScript was happy because it was checking the
// copy.

type Bounds = [number, number, number, number, number, number]

/**
 * A mapper that accepts clipping planes. Both volume and surface mappers
 * inherit these from AbstractMapper; only the volume mapper needed the OpenGL
 * shader support, which it has.
 */
interface ClippableMapper {
  addClippingPlane(plane: unknown): boolean
  removeAllClippingPlanes(): boolean
  getBounds(): number[]
  modified(): void
}

interface PlaneVisual {
  source: ReturnType<typeof vtkPlaneSource.newInstance>
  mapper: ReturnType<typeof vtkMapper.newInstance>
  actor: ReturnType<typeof vtkActor.newInstance>
}

interface SurfaceEntry {
  actor: ReturnType<typeof vtkActor.newInstance>
  mapper: ReturnType<typeof vtkMapper.newInstance>
  reader: { delete(): void }
  labels: VertexLabels | null
  /** Per-vertex RGB, allocated on the first selection. */
  colours: Uint8Array | null
  colourArray: ReturnType<typeof vtkDataArray.newInstance> | null
}

interface Ctx {
  generic: ReturnType<typeof vtkGenericRenderWindow.newInstance>
  renderer: ReturnType<ReturnType<typeof vtkGenericRenderWindow.newInstance>['getRenderer']>
  renderWindow: ReturnType<ReturnType<typeof vtkGenericRenderWindow.newInstance>['getRenderWindow']>
  volumeMapper: ClippableMapper | null
  volume: ReturnType<typeof vtkVolume.newInstance> | null
  range: [number, number]
  bounds: Bounds
  planes: Record<Axis, ReturnType<typeof vtkPlane.newInstance>>
  visuals: Record<Axis, PlaneVisual>
  surfaces: Map<string, SurfaceEntry>
  marker: ReturnType<typeof vtkOrientationMarkerWidget.newInstance> | null
  axes: ReturnType<typeof vtkAxesActor.newInstance> | null
  /** False until something has been framed, so the first load can reset the camera. */
  framed: boolean
}

const AXIS_INDEX: Record<Axis, 0 | 1 | 2> = { x: 0, y: 1, z: 2 }
const AXES: Axis[] = ['x', 'y', 'z']

/**
 * How far the pointer may travel and still count as a click. Orbiting is a drag
 * that starts and ends on the model, so without this every rotation would
 * select whatever was under the button when it came up.
 */
const CLICK_SLOP_PX = 4

/** Labels other than the selected one, so a pick reads as one structure. */
const DIMMED: [number, number, number] = [104, 112, 122]

async function fetchBytes(ref: string): Promise<ArrayBuffer> {
  const response = await fetchArtifact(artifactUrl(ref))
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`)
  return response.arrayBuffer()
}

/**
 * Fetch the per-vertex labels for a surface, checked against the mesh it
 * belongs to.
 *
 * The values arrive as a bare binary array with the legend and the per-label
 * tallies in a companion JSON. They cannot ride along in the PLY: the reader
 * here ignores face properties and cell data without saying so, so a mesh that
 * looked labelled would come back unlabelled and silent.
 *
 * The length check is the point of this function. One int32 per vertex is the
 * whole contract, and a sidecar from a different extraction would still parse
 * cleanly and then label the wrong vertices.
 */
async function loadVertexLabels(
  surface: SceneSurface,
  pointCount: number,
): Promise<VertexLabels | null> {
  if (!surface.labelsRef) return null

  const blob = await fetchBytes(surface.labelsRef)
  if (blob.byteLength !== pointCount * 4) {
    // Said out loud, not silently dropped. Rejecting the array is what keeps a
    // click from labelling the wrong vertices, but the visible symptom is only
    // "clicking does nothing", which is indistinguishable from a broken picker.
    console.warn(
      `[scene] ignoring vertex labels for ${surface.id}: ${blob.byteLength / 4} ` +
        `labels for a mesh of ${pointCount} vertices`,
    )
    return null
  }

  let header: { legend?: Record<string, string>; counts?: Record<string, number> } | null = null
  if (surface.labelsHeaderRef) {
    const response = await fetchArtifact(artifactUrl(surface.labelsHeaderRef))
    if (response.ok) header = await response.json()
  }

  return {
    values: new Int32Array(blob),
    legend: header?.legend ?? {},
    counts: header?.counts ?? {},
  }
}

/**
 * Colour and opacity ramps over the selected density window.
 *
 * Anchored on the user's thresholds rather than the raw data range, so raising
 * the low threshold is what removes soft tissue and leaves bone standing.
 */
function buildTransferFunctions(
  range: [number, number],
  density: [number, number],
  cutAboveHigh: boolean,
) {
  const [min, max] = range
  const span = max - min || 1
  const lo = min + span * density[0]
  const hi = min + span * density[1]
  const width = hi - lo || span
  const at = (fraction: number) => lo + width * fraction

  const colour = vtkColorTransferFunction.newInstance()
  colour.addRGBPoint(lo, 0, 0, 0)
  colour.addRGBPoint(at(0.25), 0.34, 0.27, 0.23)
  colour.addRGBPoint(at(0.5), 0.72, 0.67, 0.58)
  colour.addRGBPoint(at(0.75), 0.93, 0.91, 0.86)
  colour.addRGBPoint(hi, 1, 1, 1)

  // Weighted towards the top of the window. A ramp that rises early makes soft
  // tissue saturate the ray before it ever reaches bone, which reads as a solid
  // block. Pair this with the opacity unit distance when tuning.
  const opacity = vtkPiecewiseFunction.newInstance()
  opacity.addPoint(lo, 0)
  opacity.addPoint(at(0.35), 0.01)
  opacity.addPoint(at(0.6), 0.05)
  opacity.addPoint(at(0.85), 0.32)
  opacity.addPoint(hi, 0.8)

  if (cutAboveHigh) {
    // Close the window off again. A piecewise function clamps beyond its last
    // point, so without this everything denser than `hi` keeps the peak opacity
    // and enamel, restorations and metal sit in front of the bone.
    opacity.addPoint(hi + width * 0.001, 0)
  }

  return { colour, opacity, low: lo, high: hi }
}

function createPlaneVisuals(renderer: Ctx['renderer']): Record<Axis, PlaneVisual> {
  const visuals = {} as Record<Axis, PlaneVisual>

  for (const axis of AXES) {
    const source = vtkPlaneSource.newInstance()
    // Default is a 10x10 tessellation; a flat quad needs no subdivision.
    source.setXResolution(1)
    source.setYResolution(1)

    const mapper = vtkMapper.newInstance()
    mapper.setInputConnection(source.getOutputPort())

    const actor = vtkActor.newInstance()
    actor.setMapper(mapper)
    actor.setVisibility(false)

    const property = actor.getProperty()
    property.setColor(...AXIS_COLORS[axis])
    property.setOpacity(0.16)
    // Drawn from the kept side only, so which half survives is legible without
    // a separate normal arrow — you cannot see the quad from the clipped side.
    property.setBackfaceCulling(true)
    property.setFrontfaceCulling(false)

    // Deliberately NOT registered as clippable: a plane visual that clipped
    // itself would vanish the moment it was switched on.
    renderer.addActor(actor)
    visuals[axis] = { source, mapper, actor }
  }

  return visuals
}

function canvasOf(
  generic: ReturnType<typeof vtkGenericRenderWindow.newInstance>,
): HTMLCanvasElement | null {
  const view = generic.getRenderWindow().getViews()[0]
  return (view?.getCanvas?.() as HTMLCanvasElement | undefined) ?? null
}

/**
 * Device pixels per CSS pixel. vtk reports pointer positions in canvas pixels
 * with y measured up from the bottom, so a click's position in the page needs
 * both the scale and the flip undone.
 */
function devicePixelScale(
  generic: ReturnType<typeof vtkGenericRenderWindow.newInstance>,
): number {
  const canvas = canvasOf(generic)
  const rect = canvas?.getBoundingClientRect()
  if (!canvas || !rect || rect.width === 0) return window.devicePixelRatio || 1
  return canvas.width / rect.width
}

/**
 * Build the world-space ray through a pixel.
 *
 * Mirrors what vtk's own picker does with the camera and the viewport, because
 * the display-to-world conversions are only correct in that order: display
 * coordinates are framebuffer pixels with y up, and both the flip and the
 * device pixel ratio are already applied by the time an interactor event
 * arrives.
 */
function rayThrough(
  ctx: Ctx,
  screenX: number,
  screenY: number,
): Ray | null {
  const view = ctx.renderWindow.getViews()[0]
  const camera = ctx.renderer.getActiveCamera()
  if (!view || !camera) return null

  const dims = view.getViewportSize(ctx.renderer)
  if (!dims || dims[0] === 0 || dims[1] === 0) return null
  const aspect = dims[0] / dims[1]

  // The z comes from the focal point, which is what puts the sample point on
  // the focal plane; any z along the same ray gives the same direction.
  const focus = camera.getFocalPoint()
  const projected = ctx.renderer.worldToNormalizedDisplay(
    focus[0],
    focus[1],
    focus[2],
    aspect,
  )
  const display = view.normalizedDisplayToDisplay(projected[0], projected[1], projected[2])
  const normalized = view.displayToNormalizedDisplay(screenX, screenY, display[2])
  const world = ctx.renderer.normalizedDisplayToWorld(
    normalized[0],
    normalized[1],
    normalized[2],
    aspect,
  )

  const origin = camera.getPosition()
  const vx = world[0] - origin[0]
  const vy = world[1] - origin[1]
  const vz = world[2] - origin[2]
  const length = Math.hypot(vx, vy, vz)
  if (length === 0) return null

  return {
    origin: [origin[0], origin[1], origin[2]],
    direction: [vx / length, vy / length, vz / length],
  }
}

/**
 * Visibility test for a surface's cutting planes, or undefined when they do not
 * apply to it.
 *
 * A parked plane — one switched off — sits a full extent outside the data, so
 * every point still passes it and no special case is needed for disabled axes.
 */
function clipVisibility(ctx: Ctx, clipped: boolean): Visibility | undefined {
  if (!clipped) return undefined

  const planes = AXES.map((axis) => ctx.planes[axis]).map((plane) => ({
    normal: plane.getNormal() as ArrayLike<number>,
    origin: plane.getOrigin() as ArrayLike<number>,
  }))

  return (x, y, z) =>
    planes.every(
      ({ normal, origin }) =>
        normal[0] * (x - origin[0]) +
          normal[1] * (y - origin[1]) +
          normal[2] * (z - origin[2]) >=
        0,
    )
}

/** Positions and cells of a mesh, as vtk stores them. */
interface MeshArrays {
  positions: ArrayLike<number>
  polys: ArrayLike<number>
}

function meshArrays(mapper: unknown): MeshArrays | null {
  const data = (
    mapper as { getInputData(): {
      getPoints(): { getData(): ArrayLike<number> } | null
      getPolys(): { getData(): ArrayLike<number> } | null
    } | null }
  ).getInputData()
  const points = data?.getPoints()?.getData()
  const polys = data?.getPolys()?.getData()
  if (!points || !polys) return null
  return { positions: points, polys }
}

/** Ray-cast the labelled meshes and report the nearest structure. */
function selectAt(
  ctx: Ctx | null,
  model: SceneModel,
  screenX: number,
  screenY: number,
  report: (selection: SceneSelection | null) => void,
): void {
  if (!ctx) return

  const ray = rayThrough(ctx, screenX, screenY)
  if (!ray) {
    console.warn('[pick] no ray: the viewport has no size yet')
    report(null)
    return
  }

  const considered: string[] = []
  const skipped: string[] = []

  // Only meshes that can name what was hit. The volume is not an actor, and an
  // unlabelled surface has nothing to report, so neither is allowed to win.
  let best: { entry: SurfaceEntry; key: string; label: number; distance: number } | null = null

  for (const [id, entry] of ctx.surfaces) {
    const label = id.slice(0, 8)
    if (!entry.labels) {
      skipped.push(`${label} (no labels)`)
      continue
    }
    if (!entry.actor.getVisibility()) {
      skipped.push(`${label} (hidden)`)
      continue
    }

    const bounds = entry.mapper.getBounds()
    if (!intersectBox(ray, bounds)) {
      skipped.push(`${label} (bounds miss)`)
      continue
    }

    const arrays = meshArrays(entry.mapper)
    if (!arrays) {
      skipped.push(`${label} (no geometry)`)
      continue
    }

    // Geometry the cutting planes have removed must not answer a click: it is
    // not on screen, so picking it would name a structure the user cannot see.
    const surface = model.surfaces.find((s) => s.id === id)
    const hit = intersectMesh(
      ray,
      arrays.positions,
      arrays.polys,
      clipVisibility(ctx, surface?.clipped ?? false),
    )
    if (!hit) {
      skipped.push(`${label} (bounds hit, no visible triangle)`)
      continue
    }

    // Any vertex of the triangle answers for it: each label is extracted as its
    // own closed mesh, so a triangle never straddles two of them.
    const pointId = arrays.polys[hit.triangle * 4 + 1]
    const found = entry.labels.values[pointId] ?? 0
    considered.push(
      `${label} t=${hit.distance.toFixed(1)} vertex=${pointId} label=${found}`,
    )

    if (found === 0) continue
    if (!best || hit.distance < best.distance) {
      best = { entry, key: id, label: found, distance: hit.distance }
    }
  }

  console.groupCollapsed(
    `[pick] ${considered.length} hit / ${skipped.length} skipped at (${Math.round(screenX)}, ${Math.round(screenY)})`,
  )
  console.log(
    'ray origin',
    ray.origin.map((v) => v.toFixed(1)),
    'direction',
    ray.direction.map((v) => v.toFixed(3)),
  )
  console.log('skipped', skipped)
  console.log('hits', considered)
  if (best) {
    const name = best.entry.labels?.legend[String(best.label)] ?? `Label ${best.label}`
    console.log('selected', name, 'label', best.label, 'at', best.distance.toFixed(1), 'mm')
  } else {
    console.log('selected nothing')
  }
  console.groupEnd()

  if (!best) {
    report(null)
    return
  }

  const rect = canvasOf(ctx.generic)?.getBoundingClientRect()
  const scale = devicePixelScale(ctx.generic)
  const legend = best.entry.labels?.legend[String(best.label)]

  report({
    surfaceId: best.key,
    label: best.label,
    name: legend ?? `Label ${best.label}`,
    vertices: best.entry.labels?.counts[String(best.label)] ?? 0,
    x: rect ? screenX / scale : screenX,
    y: rect ? rect.height - screenY / scale : screenY,
  })
}

function applyClipFlags(ctx: Ctx, model: SceneModel) {
  const planes = AXES.map((axis) => ctx.planes[axis])

  // The volume always follows the planes; surfaces opt in individually.
  ctx.volumeMapper?.removeAllClippingPlanes()
  for (const plane of planes) ctx.volumeMapper?.addClippingPlane(plane)

  for (const [id, entry] of ctx.surfaces) {
    const surface = model.surfaces.find((s) => s.id === id)
    const mapper = entry.mapper as unknown as ClippableMapper
    mapper.removeAllClippingPlanes()
    if (surface?.clipped) {
      for (const plane of planes) mapper.addClippingPlane(plane)
    }
  }
}

function applyPlanes(ctx: Ctx, settings: ViewportSettings) {
  const lo = (i: number) => ctx.bounds[i * 2]
  const hi = (i: number) => ctx.bounds[i * 2 + 1]
  const diagonal = Math.hypot(hi(0) - lo(0), hi(1) - lo(1), hi(2) - lo(2)) || 1

  for (const axis of AXES) {
    const config = settings.planes[axis]
    const i = AXIS_INDEX[axis]
    const j = (i + 1) % 3
    const k = (i + 2) % 3

    const low = lo(i)
    const span = hi(i) - low || 1

    // A clipping plane keeps the half-space the normal points into.
    const normal: [number, number, number] = [0, 0, 0]
    normal[i] = config.flip ? -1 : 1

    // Disabled planes are parked a full extent outside the data, so they clip
    // nothing and nothing has to be added or removed to toggle one.
    const cut = config.enabled
      ? low + span * config.position
      : config.flip
        ? hi(i) + span
        : low - span

    const origin: [number, number, number] = [0, 0, 0]
    origin[i] = cut
    ctx.planes[axis].setNormal(normal)
    ctx.planes[axis].setOrigin(origin)

    // Quad spanning the other two axes, lifted a hair into the kept half so it
    // does not z-fight with the clipped surface. `showPlane` hides the graphic
    // without disarming the cut.
    const visual = ctx.visuals[axis]
    const visible = config.enabled && config.showPlane
    visual.actor.setVisibility(visible)
    if (!visible) continue

    const at = cut + (config.flip ? -1 : 1) * diagonal * 0.002
    const corner: [number, number, number] = [0, 0, 0]
    corner[i] = at
    corner[j] = lo(j)
    corner[k] = lo(k)

    const alongJ: [number, number, number] = [0, 0, 0]
    alongJ[i] = at
    alongJ[j] = hi(j)
    alongJ[k] = lo(k)

    const alongK: [number, number, number] = [0, 0, 0]
    alongK[i] = at
    alongK[j] = lo(j)
    alongK[k] = hi(k)

    visual.source.setOrigin(corner)
    // Swapping the two edge points reverses the winding, which reverses the
    // face normal, which is what flips the single-sided rendering.
    visual.source.setPoint1(config.flip ? alongK : alongJ)
    visual.source.setPoint2(config.flip ? alongJ : alongK)
    visual.source.modified()
  }
}

export function Scene({
  model,
  settings,
  selection,
  onSelect,
}: {
  model: SceneModel
  settings: ViewportSettings
  selection: SceneSelection | null
  onSelect: (selection: SceneSelection | null) => void
}) {
  const containerRef = useRef<HTMLDivElement>(null)
  const ctxRef = useRef<Ctx | null>(null)
  const [status, setStatus] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [revision, setRevision] = useState(0)

  // The pick handlers are bound to the interactor once, at mount, so they
  // cannot close over props that change every render.
  const onSelectRef = useRef(onSelect)
  onSelectRef.current = onSelect
  // Read for each surface's clip flag, so a click ignores geometry the cutting
  // planes have taken off the screen.
  const modelRef = useRef(model)
  modelRef.current = model

  const volumeKey = model.volume ? `${model.volume.headerRef}|${model.volume.binRef}` : ''
  const surfaceKey = model.surfaces.map((s) => `${s.id}:${s.meshRef}`).join(',')

  // ── render window: created once, content managed below ───────────────────
  useEffect(() => {
    const container = containerRef.current
    if (!container) return

    const generic = vtkGenericRenderWindow.newInstance()
    generic.setContainer(container)
    generic.resize()

    const renderer = generic.getRenderer()
    const renderWindow = generic.getRenderWindow()

    ctxRef.current = {
      generic,
      renderer,
      renderWindow,
      volumeMapper: null,
      volume: null,
      range: [0, 1],
      bounds: [0, 1, 0, 1, 0, 1],
      planes: { x: vtkPlane.newInstance(), y: vtkPlane.newInstance(), z: vtkPlane.newInstance() },
      visuals: createPlaneVisuals(renderer),
      surfaces: new Map(),
      marker: null,
      axes: null,
      framed: false,
    }

    const interactor = renderWindow.getInteractor()
    let pressedAt: { x: number; y: number } | null = null

    const onPress = (event: { position: { x: number; y: number } }) => {
      pressedAt = { x: event.position.x, y: event.position.y }
    }

    const onRelease = (event: { position: { x: number; y: number } }) => {
      const start = pressedAt
      pressedAt = null
      if (!start) return

      const { x, y } = event.position
      const travelled = Math.hypot(x - start.x, y - start.y)
      if (travelled > CLICK_SLOP_PX * devicePixelScale(generic)) return

      selectAt(ctxRef.current, modelRef.current, x, y, (picked) =>
        onSelectRef.current(picked),
      )
    }

    const subscriptions = [
      interactor.onLeftButtonPress(onPress),
      interactor.onLeftButtonRelease(onRelease),
    ]

    const observer = new ResizeObserver(() => {
      generic.resize()
      renderWindow.render()
    })
    observer.observe(container)

    setRevision((value) => value + 1)

    return () => {
      observer.disconnect()
      for (const subscription of subscriptions) subscription.unsubscribe()
      ctxRef.current?.marker?.setEnabled(false)
      generic.delete()
      ctxRef.current = null
    }
  }, [])

  // ── volume ───────────────────────────────────────────────────────────────
  useEffect(() => {
    const ctx = ctxRef.current
    if (!ctx) return

    if (ctx.volume) {
      ctx.renderer.removeVolume(ctx.volume)
      ctx.volume.delete()
      ctx.volume = null
      ctx.volumeMapper = null
    }
    if (!model.volume) return

    let cancelled = false
    setStatus('Loading volume…')

    const load = async () => {
      const header: VolumeHeader = await (await fetchArtifact(artifactUrl(model.volume!.headerRef))).json()
      const buffer = await fetchBytes(model.volume!.binRef)
      if (cancelled) return

      const imageData = vtkImageData.newInstance()
      imageData.setDimensions(header.dims)
      imageData.setSpacing(header.spacing)
      imageData.setOrigin(header.origin)
      // Without this the volume is drawn as if every series ran the same way.
      // They do not: a study stored with its rows or slices reversed carries a
      // -1 in its direction, and ignoring it draws that study mirrored while the
      // surfaces extracted from the same data — which do honour it — sit in the
      // right place. vtk.js reads these nine numbers as the x, y and z axis
      // vectors and folds them into the image's index-to-world matrix, which is
      // the same convention the backend writes them in.
      imageData.setDirection(header.direction)

      const scalars = vtkDataArray.newInstance({
        name: 'scalars',
        values: new Float32Array(buffer),
        numberOfComponents: 1,
      })
      imageData.getPointData().setScalars(scalars)

      const mapper = vtkVolumeMapper.newInstance()
      // One sample per voxel. Opacity is set separately by the scalar opacity
      // unit distance, and only the RATIO of the two affects how solid the
      // volume looks, so this stays a pure quality knob.
      mapper.setSampleDistance((header.spacing[0] + header.spacing[1] + header.spacing[2]) / 3)
      // Auto-adjust coarsens sampling during interaction, which changes the
      // ratio and makes the volume fade while orbiting.
      mapper.setAutoAdjustSampleDistances(false)
      mapper.setInputData(imageData)

      const volume = vtkVolume.newInstance()
      volume.setMapper(mapper)

      ctx.renderer.addVolume(volume)
      ctx.volumeMapper = mapper as unknown as ClippableMapper
      ctx.volume = volume
      ctx.range = header.value_range
      ctx.bounds = mapper.getBounds() as Bounds
      ctx.framed = false
      setStatus(null)
      setRevision((value) => value + 1)
    }

    load().catch((cause: unknown) => {
      if (!cancelled) setError(cause instanceof Error ? cause.message : String(cause))
    })

    return () => {
      cancelled = true
    }
  }, [volumeKey])

  // ── surfaces: reconcile actors against the requested list ────────────────
  useEffect(() => {
    const ctx = ctxRef.current
    if (!ctx) return

    let cancelled = false

    const reconcile = async () => {
      const wanted = new Set(model.surfaces.map((s) => s.id))

      for (const [id, entry] of [...ctx.surfaces]) {
        if (wanted.has(id) && model.surfaces.some((s) => s.id === id && s.meshRef)) continue
        ctx.renderer.removeActor(entry.actor)
        entry.actor.delete()
        entry.mapper.delete()
        entry.reader.delete()
        ctx.surfaces.delete(id)
      }

      for (const surface of model.surfaces) {
        if (ctx.surfaces.has(surface.id)) continue
        setStatus('Loading surface…')

        const buffer = await fetchBytes(surface.meshRef)
        if (cancelled) return

        const reader =
          surface.format === 'stl' ? vtkSTLReader.newInstance() : vtkPLYReader.newInstance()
        reader.parseAsArrayBuffer(buffer)

        const data = reader.getOutputData(0)
        const mapper = vtkMapper.newInstance()
        mapper.setInputData(data)

        const actor = vtkActor.newInstance()
        actor.setMapper(mapper)
        actor.getProperty().setColor(...surface.color)
        actor.getProperty().setOpacity(0.95)
        actor.getProperty().setAmbient(0.3)
        actor.getProperty().setDiffuse(0.8)
        actor.getProperty().setSpecular(0.1)

        const labels = await loadVertexLabels(surface, data.getPoints().getNumberOfPoints())
        if (cancelled) return

        ctx.renderer.addActor(actor)
        ctx.surfaces.set(surface.id, {
          actor,
          mapper,
          reader,
          labels,
          colours: null,
          colourArray: null,
        } as unknown as SurfaceEntry)

        // The first thing on screen sets the camera; adding more later must not
        // yank the view the user has framed.
        if (!ctx.framed && !ctx.volume) {
          ctx.bounds = mapper.getBounds() as Bounds
          ctx.framed = false
        }
        setStatus(null)
      }

      if (!cancelled) setRevision((value) => value + 1)
    }

    void reconcile().catch((cause: unknown) => {
      if (!cancelled) setError(cause instanceof Error ? cause.message : String(cause))
    })

    return () => {
      cancelled = true
    }
  }, [surfaceKey])

  // ── frame on first content ───────────────────────────────────────────────
  useEffect(() => {
    const ctx = ctxRef.current
    if (!ctx || ctx.framed) return
    if (!ctx.volume && ctx.surfaces.size === 0) return
    ctx.renderer.resetCamera()
    ctx.renderWindow.render()
    ctx.framed = true
  }, [revision])

  // ── visibility ───────────────────────────────────────────────────────────
  useEffect(() => {
    const ctx = ctxRef.current
    if (!ctx) return
    ctx.volume?.setVisibility(settings.showVolume)
    for (const surface of model.surfaces) {
      ctx.surfaces.get(surface.id)?.actor.setVisibility(surface.visible)
    }
    ctx.renderWindow.render()
  }, [model, settings.showVolume, revision])

  // ── selection: recolour the picked structure, dim the rest ───────────────
  useEffect(() => {
    const ctx = ctxRef.current
    if (!ctx) return

    for (const [id, entry] of ctx.surfaces) {
      const picked = selection?.surfaceId === id ? selection : null
      const surface = model.surfaces.find((s) => s.id === id)
      const values = entry.labels?.values

      if (!picked || !values || !surface) {
        entry.mapper.setScalarVisibility(false)
        entry.mapper.modified()
        continue
      }

      if (!entry.colours || !entry.colourArray) {
        entry.colours = new Uint8Array(values.length * 3)
        entry.colourArray = vtkDataArray.newInstance({
          name: 'selection colours',
          values: entry.colours,
          numberOfComponents: 3,
        })
        // Replaces the reader's scalars rather than joining them: the mapper
        // looks up point-data scalars by name, and a PLY with its own scalars
        // would otherwise keep winning.
        ;(
          entry.mapper as unknown as {
            getInputData(): { getPointData(): { setScalars(array: unknown): void } }
          }
        ).getInputData().getPointData().setScalars(entry.colourArray)
      }

      // The result's own colour, normalised to full brightness, so the picked
      // structure still reads as belonging to that result.
      const peak = Math.max(...surface.color, 0.001)
      const accent = surface.color.map((c) => Math.round(255 * Math.min(1, (c / peak) * 0.95 + 0.05)))
      const colours = entry.colours

      for (let i = 0; i < values.length; i += 1) {
        const chosen = values[i] === picked.label
        colours[i * 3] = chosen ? accent[0] : DIMMED[0]
        colours[i * 3 + 1] = chosen ? accent[1] : DIMMED[1]
        colours[i * 3 + 2] = chosen ? accent[2] : DIMMED[2]
      }

      // The mapper caches its colour build on the array's modification time,
      // and writing into the typed array does not move it — without this the
      // colours on screen stay those of the previous pick.
      entry.colourArray.modified()
      entry.mapper.setScalarVisibility(true)
      entry.mapper.setScalarModeToUsePointData()
      entry.mapper.setColorModeToDirectScalars()
      entry.mapper.modified()
    }

    ctx.renderWindow.render()
  }, [selection, model, revision])

  // ── background ───────────────────────────────────────────────────────────
  useEffect(() => {
    const ctx = ctxRef.current
    if (!ctx) return
    ctx.renderer.setBackground(...hexToRgb01(backgroundHex(settings)))
    ctx.renderWindow.render()
  }, [settings.background, settings.customBackground, revision])

  // ── orientation marker ───────────────────────────────────────────────────
  useEffect(() => {
    const ctx = ctxRef.current
    if (!ctx) return

    if (!settings.showOrientation) {
      ctx.marker?.setEnabled(false)
      return
    }

    if (!ctx.marker) {
      // vtk.js defaults are X red, Y yellow, Z green. Yellow is hard to read,
      // especially over a light background, so Y becomes blue and Z takes the
      // green — red/green/blue with blue on Y.
      const axes = vtkAxesActor.newInstance()
      axes.setYAxisColor([40, 110, 235])
      axes.setZAxisColor([40, 190, 100])
      ctx.axes = axes
      try {
        const marker = vtkOrientationMarkerWidget.newInstance({
          actor: axes,
          interactor: ctx.renderWindow.getInteractor(),
          parentRenderer: ctx.renderer,
          viewportCorner: Corners.TOP_LEFT,
          viewportSize: 0.14,
        })
        marker.setMinPixelSize(80)
        marker.setMaxPixelSize(160)
        ctx.marker = marker
      } catch {
        // Decoration; never let it take the viewport down.
        ctx.marker = null
        return
      }
    }
    ctx.marker?.setEnabled(true)
    ctx.renderWindow.render()
  }, [settings.showOrientation, revision])

  // ── transfer function ────────────────────────────────────────────────────
  useEffect(() => {
    const ctx = ctxRef.current
    if (!ctx?.volume) return

    const { colour, opacity } = buildTransferFunctions(
      ctx.range,
      settings.density,
      settings.cutAboveHigh,
    )
    const property = ctx.volume.getProperty()
    property.setRGBTransferFunction(0, colour)
    property.setScalarOpacity(0, opacity)
    property.setScalarOpacityUnitDistance(0, settings.opacityUnitDistance)
    property.setInterpolationTypeToLinear()
    property.setShade(true)
    property.setAmbient(0.25)
    property.setDiffuse(0.75)
    property.setSpecular(0.15)
    ctx.renderWindow.render()
  }, [settings.density, settings.cutAboveHigh, settings.opacityUnitDistance, revision])

  // ── clipping ─────────────────────────────────────────────────────────────
  useEffect(() => {
    const ctx = ctxRef.current
    if (!ctx) return
    applyPlanes(ctx, settings)
    applyClipFlags(ctx, model)
    for (const entry of ctx.surfaces.values()) {
      ;(entry.mapper as unknown as ClippableMapper).modified()
    }
    ctx.volumeMapper?.modified()
    ctx.renderWindow.render()
    // Depends on `model`, not on `surfaceKey`: the key identifies which meshes
    // are loaded, and toggling a result's clip flag changes none of it, so
    // keying on it would leave the planes on a surface just exempted.
  }, [settings.planes, model, revision])

  return (
    <div style={{ position: 'absolute', inset: 0 }}>
      <div className="scene-host" ref={containerRef} />
      {error && <div className="empty-state error-text">{error}</div>}
      {!error && status && <div className="empty-state">{status}</div>}
    </div>
  )
}
