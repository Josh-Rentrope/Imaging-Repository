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

import { artifactUrl } from '../lib/api'
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
}

export interface SceneModel {
  volume: SceneVolume | null
  surfaces: SceneSurface[]
}

interface VolumeHeader {
  dims: [number, number, number]
  spacing: [number, number, number]
  origin: [number, number, number]
  byte_length: number
  value_range: [number, number]
  window_center: number | null
  window_width: number | null
}

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

async function fetchBytes(ref: string): Promise<ArrayBuffer> {
  const response = await fetch(artifactUrl(ref))
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`)
  return response.arrayBuffer()
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

export function Scene({ model, settings }: { model: SceneModel; settings: ViewportSettings }) {
  const containerRef = useRef<HTMLDivElement>(null)
  const ctxRef = useRef<Ctx | null>(null)
  const [status, setStatus] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [revision, setRevision] = useState(0)

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

    const observer = new ResizeObserver(() => {
      generic.resize()
      renderWindow.render()
    })
    observer.observe(container)

    setRevision((value) => value + 1)

    return () => {
      observer.disconnect()
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
      const header: VolumeHeader = await (await fetch(artifactUrl(model.volume!.headerRef))).json()
      const buffer = await fetchBytes(model.volume!.binRef)
      if (cancelled) return

      const imageData = vtkImageData.newInstance()
      imageData.setDimensions(header.dims)
      imageData.setSpacing(header.spacing)
      imageData.setOrigin(header.origin)

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

        const mapper = vtkMapper.newInstance()
        mapper.setInputData(reader.getOutputData(0))

        const actor = vtkActor.newInstance()
        actor.setMapper(mapper)
        actor.getProperty().setColor(...surface.color)
        actor.getProperty().setOpacity(0.95)
        actor.getProperty().setAmbient(0.3)
        actor.getProperty().setDiffuse(0.8)
        actor.getProperty().setSpecular(0.1)

        ctx.renderer.addActor(actor)
        ctx.surfaces.set(surface.id, { actor, mapper, reader } as unknown as SurfaceEntry)

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
    for (const surface of model.surfaces) {
      ctx.surfaces.get(surface.id)?.actor.setVisibility(surface.visible)
    }
    ctx.renderWindow.render()
  }, [model.surfaces, revision])

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
  }, [settings.planes, surfaceKey, volumeKey, revision])

  return (
    <div style={{ position: 'absolute', inset: 0 }}>
      <div className="scene-host" ref={containerRef} />
      {error && <div className="empty-state error-text">{error}</div>}
      {!error && status && <div className="empty-state">{status}</div>}
    </div>
  )
}
