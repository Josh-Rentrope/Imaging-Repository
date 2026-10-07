import { useEffect, useRef, useState } from 'react'

// Side-effect imports: these register the rendering backends. Without them the
// mappers construct but nothing draws.
import '@kitware/vtk.js/Rendering/Profiles/Geometry'
import '@kitware/vtk.js/Rendering/Profiles/Volume'

import vtkDataArray from '@kitware/vtk.js/Common/Core/DataArray'
import vtkPiecewiseFunction from '@kitware/vtk.js/Common/DataModel/PiecewiseFunction'
import vtkImageData from '@kitware/vtk.js/Common/DataModel/ImageData'
import vtkActor from '@kitware/vtk.js/Rendering/Core/Actor'
import vtkColorTransferFunction from '@kitware/vtk.js/Rendering/Core/ColorTransferFunction'
import vtkMapper from '@kitware/vtk.js/Rendering/Core/Mapper'
import vtkVolume from '@kitware/vtk.js/Rendering/Core/Volume'
import vtkVolumeMapper from '@kitware/vtk.js/Rendering/Core/VolumeMapper'
import vtkPLYReader from '@kitware/vtk.js/IO/Geometry/PLYReader'
import vtkSTLReader from '@kitware/vtk.js/IO/Geometry/STLReader'
import vtkGenericRenderWindow from '@kitware/vtk.js/Rendering/Misc/GenericRenderWindow'

import { artifactUrl } from '../lib/api'

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

async function fetchBytes(ref: string): Promise<ArrayBuffer> {
  const response = await fetch(artifactUrl(ref))
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`)
  return response.arrayBuffer()
}

/**
 * Grey ramp with an opacity ramp over the display window.
 *
 * The opacity curve is deliberately conservative at the low end: soft tissue and
 * air otherwise fill the whole view and hide the teeth. Bone, which is what a
 * dental volume is usually opened for, sits at the top of the range.
 */
function applyTransferFunctions(
  volume: ReturnType<typeof vtkVolume.newInstance>,
  header: VolumeHeader,
) {
  const [min, max] = header.value_range
  const lo = header.window_center != null && header.window_width != null
    ? header.window_center - header.window_width / 2
    : min
  const hi = header.window_center != null && header.window_width != null
    ? header.window_center + header.window_width / 2
    : max

  const span = hi - lo || 1
  const t = (fraction: number) => lo + span * fraction

  const colour = vtkColorTransferFunction.newInstance()
  colour.addRGBPoint(lo, 0, 0, 0)
  colour.addRGBPoint(t(0.3), 0.35, 0.28, 0.24)
  colour.addRGBPoint(t(0.55), 0.72, 0.67, 0.58)
  colour.addRGBPoint(t(0.8), 0.93, 0.91, 0.86)
  colour.addRGBPoint(hi, 1, 1, 1)

  const opacity = vtkPiecewiseFunction.newInstance()
  opacity.addPoint(lo, 0)
  opacity.addPoint(t(0.3), 0.02)
  opacity.addPoint(t(0.55), 0.14)
  opacity.addPoint(t(0.8), 0.42)
  opacity.addPoint(hi, 0.7)

  const property = volume.getProperty()
  property.setRGBTransferFunction(0, colour)
  property.setScalarOpacity(0, opacity)
  property.setInterpolationTypeToLinear()
  property.setShade(true)
  property.setAmbient(0.25)
  property.setDiffuse(0.75)
  property.setSpecular(0.15)
}

export function Scene({ input }: { input: SceneInput | null }) {
  const containerRef = useRef<HTMLDivElement>(null)
  const [status, setStatus] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const container = containerRef.current
    if (!container || !input) return

    // GenericRenderWindow owns a canvas and an OpenGL context; everything it
    // creates has to be torn down or the context leaks on every source switch.
    const generic = vtkGenericRenderWindow.newInstance()
    generic.setContainer(container)
    generic.resize()

    const renderer = generic.getRenderer()
    const renderWindow = generic.getRenderWindow()

    let disposed = false
    let observer: ResizeObserver | null = null
    const built: { delete: () => void }[] = []

    const finish = () => {
      renderer.resetCamera()
      renderWindow.render()
      if (disposed) return
      observer = new ResizeObserver(() => {
        generic.resize()
        renderWindow.render()
      })
      observer.observe(container)
    }

    async function loadVolume(volume: Extract<SceneInput, { kind: 'volume' }>) {
      setStatus('Loading volume…')
      const header: VolumeHeader = await (await fetch(artifactUrl(volume.headerRef))).json()
      const buffer = await fetchBytes(volume.binRef)
      if (disposed) return

      const imageData = vtkImageData.newInstance()
      // vtk.js takes these as a single tuple, not spread arguments.
      imageData.setDimensions(header.dims)
      imageData.setSpacing(header.spacing)
      imageData.setOrigin(header.origin)

      const scalars = vtkDataArray.newInstance({
        name: 'scalars',
        values: new Float32Array(buffer),
        numberOfComponents: 1,
      })
      imageData.getPointData().setScalars(scalars)
      built.push(imageData as unknown as { delete: () => void }, scalars as unknown as { delete: () => void })

      const mapper = vtkVolumeMapper.newInstance()
      // Half the smallest voxel dimension keeps the ray march from stepping over
      // features without wasting samples.
      mapper.setSampleDistance(Math.min(...header.spacing) * 0.5)
      mapper.setInputData(imageData)

      const volume3d = vtkVolume.newInstance()
      volume3d.setMapper(mapper)
      applyTransferFunctions(volume3d, header)
      built.push(mapper as unknown as { delete: () => void }, volume3d as unknown as { delete: () => void })

      renderer.addVolume(volume3d)
      setStatus(null)
      finish()
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
      built.push(reader as unknown as { delete: () => void }, mapper as unknown as { delete: () => void }, actor as unknown as { delete: () => void })

      renderer.addActor(actor)
      setStatus(null)
      finish()
    }

    const task = input.kind === 'volume' ? loadVolume(input) : loadSurface(input)
    task.catch((cause: unknown) => {
      if (!disposed) setError(cause instanceof Error ? cause.message : String(cause))
    })

    return () => {
      disposed = true
      observer?.disconnect()
      for (const object of built) {
        try {
          object.delete()
        } catch {
          // Already released with the render window; nothing to do.
        }
      }
      generic.delete()
    }
  }, [input])

  return (
    <div style={{ position: 'absolute', inset: 0 }}>
      <div ref={containerRef} style={{ position: 'absolute', inset: 0 }} />
      {error && <div className="empty-state error-text">{error}</div>}
      {!error && status && <div className="empty-state">{status}</div>}
    </div>
  )
}
