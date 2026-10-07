import { OrbitControls } from '@react-three/drei'
import { Canvas } from '@react-three/fiber'
import { useEffect, useMemo, useState } from 'react'
import type { BufferGeometry } from 'three'
import { PLYLoader } from 'three/examples/jsm/loaders/PLYLoader.js'

import { artifactUrl } from '../lib/api'

/**
 * Mesh rendering. Loads PLY straight from the artefact route.
 *
 * `units` governs the grid spacing: when a reconstruction has no metric anchor
 * the geometry is in arbitrary units and a millimetre grid under it would be a
 * claim the data does not support.
 */
function Mesh({ geometry, wireframe }: { geometry: BufferGeometry; wireframe: boolean }) {
  return (
    <mesh geometry={geometry}>
      <meshStandardMaterial
        color="#c8ccd1"
        roughness={0.6}
        metalness={0.02}
        wireframe={wireframe}
        // Open at the gum base and not guaranteed watertight.
        side={2}
      />
    </mesh>
  )
}

export function Scene({
  meshRef,
  units = 'arbitrary',
  wireframe = false,
}: {
  meshRef: string | null
  units?: 'mm' | 'arbitrary'
  wireframe?: boolean
}) {
  const [geometry, setGeometry] = useState<BufferGeometry | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!meshRef) {
      setGeometry(null)
      setError(null)
      return
    }

    let cancelled = false
    setLoading(true)
    setError(null)

    fetch(artifactUrl(meshRef))
      .then(async (response) => {
        if (!response.ok) throw new Error(`${response.status} ${response.statusText}`)
        return response.arrayBuffer()
      })
      .then((buffer) => {
        if (cancelled) return
        const parsed = new PLYLoader().parse(buffer)
        parsed.computeVertexNormals()
        parsed.computeBoundingBox()
        setGeometry(parsed)
      })
      .catch((cause: unknown) => {
        if (!cancelled) setError(cause instanceof Error ? cause.message : String(cause))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [meshRef])

  // Centre on the origin so orbiting pivots about the arch, not the world origin.
  const offset = useMemo<[number, number, number]>(() => {
    const box = geometry?.boundingBox
    if (!box) return [0, 0, 0]
    const centre = box.getCenter({ x: 0, y: 0, z: 0 } as never) as { x: number; y: number; z: number }
    return [-centre.x, -centre.y, -centre.z]
  }, [geometry])

  if (error) {
    return <div className="empty-state error-text">{error}</div>
  }

  if (!geometry) {
    return <div className="empty-state">{loading ? 'Loading…' : ''}</div>
  }

  return (
    <Canvas camera={{ position: [0, 55, 75], fov: 42, near: 0.1, far: 4000 }} dpr={[1, 2]}>
      <color attach="background" args={['#0d0f11']} />
      <ambientLight intensity={0.6} />
      <directionalLight position={[60, 90, 50]} intensity={1.1} />
      <directionalLight position={[-70, 35, -60]} intensity={0.35} />

      <group position={offset}>
        <Mesh geometry={geometry} wireframe={wireframe} />
        <gridHelper
          args={[units === 'mm' ? 160 : 100, units === 'mm' ? 32 : 20, '#23282d', '#191d21']}
          position={[0, -8, 0]}
        />
      </group>

      <OrbitControls makeDefault enableDamping dampingFactor={0.08} />
    </Canvas>
  )
}
