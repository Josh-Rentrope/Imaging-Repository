/**
 * Triangle-level ray casting.
 *
 * Used instead of `vtkCellPicker`, which walks every cell of every prop it is
 * given and reports only the winner. Two things follow from doing it here:
 * the caller decides which meshes are even considered, so a density-threshold
 * surface or a volume can never answer a click meant for a labelled mesh; and
 * every candidate's outcome is available to report, which is the difference
 * between "clicking does nothing" and "the ray hit X at 40 mm but X has no
 * labels".
 */

export type Vec3 = [number, number, number]

export interface Ray {
  origin: Vec3
  direction: Vec3
}

/**
 * Whether a point survives the cutting planes.
 *
 * vtk keeps the half-space a plane's normal points into, so a point is hidden
 * when it falls on the far side of any of them. Tested per hit rather than per
 * triangle: a triangle straddling the cut is partly visible, and rejecting it
 * wholesale would make everything along the cut unclickable.
 */
export type Visibility = (x: number, y: number, z: number) => boolean

export interface MeshHit {
  /** Index of the triangle, not of a vertex. */
  triangle: number
  /** Distance along the ray, in world units. */
  distance: number
}

/** Parallel rays are rejected rather than producing infinities. */
const EPSILON = 1e-9

/**
 * Slab test. Cheap enough to run before touching a mesh's triangles, which is
 * what makes it worth doing even though it costs a branch per actor.
 */
export function intersectBox(ray: Ray, bounds: ArrayLike<number>): boolean {
  let near = 0
  let far = Number.POSITIVE_INFINITY

  for (let axis = 0; axis < 3; axis += 1) {
    const low = bounds[axis * 2]
    const high = bounds[axis * 2 + 1]
    const origin = ray.origin[axis]
    const direction = ray.direction[axis]

    if (Math.abs(direction) < EPSILON) {
      // Parallel to this pair of planes: either always inside them or never.
      if (origin < low || origin > high) return false
      continue
    }

    const inverse = 1 / direction
    let t0 = (low - origin) * inverse
    let t1 = (high - origin) * inverse
    if (t0 > t1) {
      const swap = t0
      t0 = t1
      t1 = swap
    }
    if (t0 > near) near = t0
    if (t1 < far) far = t1
    if (near > far) return false
  }

  return far >= 0
}

/**
 * Nearest triangle the ray meets, or null.
 *
 * `polys` is the cell array's flat form — `[3, a, b, c, 3, a, b, c, …]` — which
 * is how vtk stores triangles and, usefully, means a hit yields the vertex
 * indices directly without building the cell links the picker needed.
 *
 * Deliberately two-sided: a mesh whose winding came out reversed would
 * otherwise become unclickable, and the back face of a structure is still that
 * structure.
 */
export function intersectMesh(
  ray: Ray,
  positions: ArrayLike<number>,
  polys: ArrayLike<number>,
  visible?: Visibility,
): MeshHit | null {
  const { origin, direction } = ray
  const ox = origin[0]
  const oy = origin[1]
  const oz = origin[2]
  const dx = direction[0]
  const dy = direction[1]
  const dz = direction[2]

  let bestTriangle = -1
  let bestDistance = Number.POSITIVE_INFINITY

  for (let at = 0; at + 3 < polys.length; at += 4) {
    // A non-triangle cell would have a different stride; the writers here only
    // emit triangles, so anything else is skipped rather than misread.
    if (polys[at] !== 3) continue

    const i0 = polys[at + 1] * 3
    const i1 = polys[at + 2] * 3
    const i2 = polys[at + 3] * 3

    const ax = positions[i0]
    const ay = positions[i0 + 1]
    const az = positions[i0 + 2]
    const bx = positions[i1]
    const by = positions[i1 + 1]
    const bz = positions[i1 + 2]
    const cx = positions[i2]
    const cy = positions[i2 + 1]
    const cz = positions[i2 + 2]

    // Möller-Trumbore, inlined: this runs once per triangle of a mesh that can
    // run to hundreds of thousands of them.
    const e1x = bx - ax
    const e1y = by - ay
    const e1z = bz - az
    const e2x = cx - ax
    const e2y = cy - ay
    const e2z = cz - az

    const px = dy * e2z - dz * e2y
    const py = dz * e2x - dx * e2z
    const pz = dx * e2y - dy * e2x

    const det = e1x * px + e1y * py + e1z * pz
    if (det > -EPSILON && det < EPSILON) continue
    const inverse = 1 / det

    const tx = ox - ax
    const ty = oy - ay
    const tz = oz - az

    const u = (tx * px + ty * py + tz * pz) * inverse
    if (u < 0 || u > 1) continue

    const qx = ty * e1z - tz * e1y
    const qy = tz * e1x - tx * e1z
    const qz = tx * e1y - ty * e1x

    const v = (dx * qx + dy * qy + dz * qz) * inverse
    if (v < 0 || u + v > 1) continue

    const distance = (e2x * qx + e2y * qy + e2z * qz) * inverse
    if (distance <= EPSILON || distance >= bestDistance) continue

    // Clipped away, so the ray carries on through it — which is what the eye
    // does, since the cutting plane has removed the surface from view.
    if (visible) {
      const hx = ox + dx * distance
      const hy = oy + dy * distance
      const hz = oz + dz * distance
      if (!visible(hx, hy, hz)) continue
    }

    bestDistance = distance
    bestTriangle = at >> 2
  }

  return bestTriangle < 0 ? null : { triangle: bestTriangle, distance: bestDistance }
}
