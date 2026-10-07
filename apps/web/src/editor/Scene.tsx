import { useEffect, useRef, useState } from 'react'

// Side-effect imports: these register the rendering backends. Without them the
// mappers construct but nothing draws.
import '@kitware/vtk.js/Rendering/Profiles/Geometry'
import '@kitware/vtk.js/Rendering/Profiles/Volume'

import vtkDataArray from '@kitware/vtk.js/Common/Core/DataArray'
import vtkPlane from '@kitware/vtk.js/Common/DataModel/Plane'
import vtkPiecewiseFunction from '@kitware/vtk.js/Common/DataModel/PiecewiseFunction'
import vtkImageData from '@kitware/vtk.js/Common/DataModel/ImageData'
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
import vtkPlaneSource from '@kitware/vtk.js/Filters/Sources/PlaneSource'

import { artifactUrl } from '../lib/api'
import {
  AXIS_COLORS,
  backgroundHex,
  hexToRgb01,
  type Axis,
  type ViewportSettings,
} from './viewportSettings'

export type SceneInput =
  | { kind: 'volume'; headerRef: string; binRef: string }
  | { kind: 'surface'; meshRef: string; format: string }

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

/** A mapper that accepts clipping planes — both volume and surface mappers do. */
interface ClippableMapper {
  addClippingPlane(plane: unknown): boolean
  getBounds(): number[]
  modified(): void
}

interface PlaneVisual {
  source: ReturnType<typeof vtkPlaneSource.newInstance>
  mapper: ReturnType<typeof vtkMapper.newInstance>
  actor: ReturnType<typeof vtkActor.newInstance>
}

interface Built {
  generic: ReturnType<typeof vtkGenericRenderWindow.newInstance>
  renderer: ReturnType<ReturnType<typeof vtkGenericRenderWindow.newInstance>['getRenderer']>
  renderWindow: ReturnType<ReturnType<typeof vtkGenericRenderWindow.newInstance>['getRenderWindow']>
  mappers: ClippableMapper[]
  volume: ReturnType<typeof vtkVolume.newInstance> | null
  range: [number, number]
  bounds: Bounds
  planes: Record<Axis, ReturnType<typeof vtkPlane.newInstance>>
  visuals: Record<Axis, PlaneVisual>
  marker: ReturnType<typeof vtkOrientationMarkerWidget.newInstance> | null
  axes: ReturnType<typeof vtkAxesActor.newInstance> | null
}

const AXIS_INDEX: Record<Axis, 0 | 1 | 2> = { x: 0, y: 1, z: 2 }

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
    // and enamel, restorations and metal sit in front of the bone you are
    // looking at. The step is narrow enough to read as a hard edge.
    opacity.addPoint(hi + width * 0.001, 0)
  }

  return { colour, opacity, low: lo, high: hi }
}

function planeVisuals(
  renderer: ReturnType<ReturnType<typeof vtkGenericRenderWindow.newInstance>['getRenderer']>,
): Record<Axis, PlaneVisual> {
  const visuals = {} as Record<Axis, PlaneVisual>

  for (const axis of ['x', 'y', 'z'] as Axis[]) {
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
    // a separate normal arrow — you simply cannot see the quad from the side
    // that is being clipped away.
    property.setBackfaceCulling(true)
    property.setFrontfaceCulling(false)

    // Deliberately NOT registered as clippable: a plane visual that clipped
    // itself would vanish the moment it was switched on.
    renderer.addActor(actor)
    visuals[axis] = { source, mapper, actor }
  }

  return visuals
}

function applyPlanes(built: Built, settings: ViewportSettings) {
  const lo = (i: number) => built.bounds[i * 2]
  const hi = (i: number) => built.bounds[i * 2 + 1]
  const diagonal = Math.hypot(hi(0) - lo(0), hi(1) - lo(1), hi(2) - lo(2)) || 1

  for (const axis of ['x', 'y', 'z'] as Axis[]) {
    const config = settings.planes[axis]
    const i = AXIS_INDEX[axis]
    const j = (i + 1) % 3
    const k = (i + 2) % 3

    const low = lo(i)
    const high = hi(i)
    const span = high - low || 1

    // A clipping plane keeps the half-space the normal points into.
    const normal: [number, number, number] = [0, 0, 0]
    normal[i] = config.flip ? -1 : 1

    // Disabled planes are parked a full extent outside the data, so they clip
    // nothing and no widget has to be added or removed to toggle one.
    const cut = config.enabled ? low + span * config.position : config.flip ? high + span : low - span

    const origin: [number, number, number] = [0, 0, 0]
    origin[i] = cut
    built.planes[axis].setNormal(normal)
    built.planes[axis].setOrigin(origin)

    // Quad spanning the other two axes, lifted a hair into the kept half so it
    // does not z-fight with the clipped surface.
    const visual = built.visuals[axis]
    visual.actor.setVisibility(config.enabled)
    if (!config.enabled) continue

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

  for (const mapper of built.mappers) {
    mapper.modified()
  }
  built.renderWindow.render()
}

export function Scene({ input, settings }: { input: SceneInput | null; settings: ViewportSettings }) {
  const containerRef = useRef<HTMLDivElement>(null)
  const builtRef = useRef<Built | null>(null)

  const [status, setStatus] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  // Bumped once the render window exists, so the settings effects below re-run
  // against the freshly built vtk objects.
  const [revision, setRevision] = useState(0)

  // ── build ────────────────────────────────────────────────────────────────
  useEffect(() => {
    const container = containerRef.current
    if (!container || !input) return

    const generic = vtkGenericRenderWindow.newInstance()
    generic.setContainer(container)
    generic.resize()

    const renderer = generic.getRenderer()
    const renderWindow = generic.getRenderWindow()

    let disposed = false
    let observer: ResizeObserver | null = null
    const owned: { delete: () => void }[] = []

    async function loadVolume(volume: Extract<SceneInput, { kind: 'volume' }>) {
      setStatus('Loading volume…')
      const header: VolumeHeader = await (await fetch(artifactUrl(volume.headerRef))).json()
      const buffer = await fetchBytes(volume.binRef)
      if (disposed) return

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
      owned.push(imageData as never, scalars as never)

      const mapper = vtkVolumeMapper.newInstance()
      // One sample per voxel. Quality is set here; opacity is set separately by
      // the scalar opacity unit distance, and only the RATIO of the two affects
      // how solid the volume looks. Keeping this at voxel size leaves the unit
      // distance free to act as a pure opacity knob.
      mapper.setSampleDistance(
        (header.spacing[0] + header.spacing[1] + header.spacing[2]) / 3,
      )
      // Auto-adjust coarsens sampling during interaction, which changes the ratio
      // and makes the volume visibly fade while orbiting.
      mapper.setAutoAdjustSampleDistances(false)
      mapper.setInputData(imageData)

      const vtkVolumeInstance = vtkVolume.newInstance()
      vtkVolumeInstance.setMapper(mapper)
      owned.push(mapper as never, vtkVolumeInstance as never)

      renderer.addVolume(vtkVolumeInstance)

      const bounds = mapper.getBounds() as Bounds
      const visuals = planeVisuals(renderer)
      for (const visual of Object.values(visuals)) {
        owned.push(visual.source as never, visual.mapper as never, visual.actor as never)
      }
      builtRef.current = {
        generic,
        renderer,
        renderWindow,
        mappers: [mapper as unknown as ClippableMapper],
        volume: vtkVolumeInstance,
        range: header.value_range,
        bounds,
        planes: {
          x: vtkPlane.newInstance(),
          y: vtkPlane.newInstance(),
          z: vtkPlane.newInstance(),
        },
        visuals,
        marker: null,
        axes: null,
      }
    }

    async function loadSurface(surface: Extract<SceneInput, { kind: 'surface' }>) {
      setStatus('Loading surface…')
      const buffer = await fetchBytes(surface.meshRef)
      if (disposed) return

      const reader = surface.format === 'stl' ? vtkSTLReader.newInstance() : vtkPLYReader.newInstance()
      reader.parseAsArrayBuffer(buffer)
      const polyData = reader.getOutputData(0)

      const mapper = vtkMapper.newInstance()
      mapper.setInputData(polyData)

      const actor = vtkActor.newInstance()
      actor.setMapper(mapper)
      actor.getProperty().setColor(0.82, 0.82, 0.85)
      actor.getProperty().setAmbient(0.25)
      actor.getProperty().setDiffuse(0.75)
      owned.push(reader as never, mapper as never, actor as never)

      renderer.addActor(actor)

      const visuals = planeVisuals(renderer)
      for (const visual of Object.values(visuals)) {
        owned.push(visual.source as never, visual.mapper as never, visual.actor as never)
      }
      builtRef.current = {
        generic,
        renderer,
        renderWindow,
        mappers: [mapper as unknown as ClippableMapper],
        volume: null,
        range: [0, 1],
        bounds: mapper.getBounds() as Bounds,
        planes: {
          x: vtkPlane.newInstance(),
          y: vtkPlane.newInstance(),
          z: vtkPlane.newInstance(),
        },
        visuals,
        marker: null,
        axes: null,
      }
    }

    const task = input.kind === 'volume' ? loadVolume(input) : loadSurface(input)
    task
      .then(() => {
        if (disposed) return
        for (const plane of Object.values(builtRef.current?.planes ?? {})) {
          for (const mapper of builtRef.current?.mappers ?? []) {
            mapper.addClippingPlane(plane)
          }
        }
        renderer.resetCamera()
        renderWindow.render()
        setStatus(null)
        setRevision((value) => value + 1)

        observer = new ResizeObserver(() => {
          generic.resize()
          renderWindow.render()
        })
        observer.observe(container)
      })
      .catch((cause: unknown) => {
        if (!disposed) setError(cause instanceof Error ? cause.message : String(cause))
      })

    return () => {
      disposed = true
      observer?.disconnect()
      builtRef.current?.marker?.setEnabled(false)
      for (const object of owned) {
        try {
          object.delete()
        } catch {
          // Already released with the render window.
        }
      }
      generic.delete()
      builtRef.current = null
    }
  }, [input])

  // ── background ───────────────────────────────────────────────────────────
  useEffect(() => {
    const built = builtRef.current
    if (!built) return
    built.renderer.setBackground(...hexToRgb01(backgroundHex(settings)))
    built.renderWindow.render()
  }, [settings.background, settings.customBackground, revision])

  // ── orientation marker ───────────────────────────────────────────────────
  useEffect(() => {
    const built = builtRef.current
    if (!built) return

    if (!settings.showOrientation) {
      built.marker?.setEnabled(false)
      return
    }

    if (!built.marker) {
      // vtk.js defaults are X red, Y yellow, Z green. Yellow is hard to read,
      // especially over a light background, so Y becomes blue and Z takes the
      // green — red/green/blue with blue on Y.
      const axes = vtkAxesActor.newInstance()
      axes.setYAxisColor([40, 110, 235])
      axes.setZAxisColor([40, 190, 100])
      built.axes = axes
      try {
        const marker = vtkOrientationMarkerWidget.newInstance({
          actor: axes,
          interactor: built.renderWindow.getInteractor(),
          parentRenderer: built.renderer,
          viewportCorner: Corners.TOP_LEFT,
          viewportSize: 0.14,
        })
        marker.setMinPixelSize(80)
        marker.setMaxPixelSize(160)
        built.marker = marker
      } catch {
        // The marker is decoration; never let it take the viewport down with it.
        built.marker = null
        return
      }
    }
    built.marker?.setEnabled(true)
    built.renderWindow.render()
  }, [settings.showOrientation, revision])

  // ── transfer function ────────────────────────────────────────────────────
  useEffect(() => {
    const built = builtRef.current
    if (!built?.volume) return

    const { colour, opacity } = buildTransferFunctions(
      built.range,
      settings.density,
      settings.cutAboveHigh,
    )
    const property = built.volume.getProperty()
    property.setRGBTransferFunction(0, colour)
    property.setScalarOpacity(0, opacity)
    // How fast opacity accumulates along a ray. This is the single strongest
    // lever on how solid the volume looks, and it is independent of the transfer
    // function itself, so it gets its own control.
    property.setScalarOpacityUnitDistance(0, settings.opacityUnitDistance)
    property.setInterpolationTypeToLinear()
    property.setShade(true)
    property.setAmbient(0.25)
    property.setDiffuse(0.75)
    property.setSpecular(0.15)
    built.renderWindow.render()
  }, [settings.density, settings.cutAboveHigh, settings.opacityUnitDistance, revision])

  // ── clipping planes ──────────────────────────────────────────────────────
  useEffect(() => {
    const built = builtRef.current
    if (!built) return
    applyPlanes(built, settings)
  }, [settings.planes, revision])

  return (
    <div style={{ position: 'absolute', inset: 0 }}>
      <div className="scene-host" ref={containerRef} />
      {error && <div className="empty-state error-text">{error}</div>}
      {!error && status && <div className="empty-state">{status}</div>}
    </div>
  )
}
