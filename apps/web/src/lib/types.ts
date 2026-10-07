/**
 * Transport types. Mirrors apps/api/app/models.py.
 *
 * Hand-written for now. Once the contracts settle, generate these from
 * contracts/*.schema.json -- hand-mirrored types drift silently, and a silent
 * drift on a clinical payload is expensive to find.
 */

/** Canonical operation names. The wire type is `string`; these are the shared spelling. */
export const Op = {
  RECTIFY: 'rectify',
  RECONSTRUCT: 'reconstruct',
  SEGMENT: 'segment',
  MEASURE: 'measure',
  ISOLATE_VOLUME: 'isolate_volume',
  ISO_SURFACE: 'iso_surface',
  DETECT_CARIES: 'detect_caries',
} as const

export const DIAGNOSTIC_OPS = new Set<string>([Op.DETECT_CARIES])

export interface Stage {
  op: string
  params?: Record<string, unknown>
}

export type JobStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'rejected'

export interface Capabilities {
  backend: string
  ops: string[]
  modalities: string[]
  max_resolution: number | null
  supports_dense_output: boolean
  notes: string | null
}

export interface CapabilitiesResponse {
  backends: Capabilities[]
  server_backend_registered: boolean
  diagnostic_ops_enabled: boolean
}

export interface HealthBackend {
  name: string
  ok: boolean
  backend: string
  detail: string | null
}

export interface ResultArtifact {
  kind:
    | 'mesh'
    | 'volume'
    | 'splat'
    | 'radiograph_overlay'
    | 'thumbnail'
    | 'report'
    /** Per-vertex labels for a mesh. A sidecar, never a property on the mesh. */
    | 'labels'
    /** A multi-label voxel mask. */
    | 'mask'
  format: string
  ref: string
  units?: 'mm' | 'arbitrary'
  bytes?: number
  vertices?: number
  triangles?: number
  /** Companion header for a binary artifact: a label legend, mask geometry, etc. */
  header_ref?: string
}

export interface SegmentationInstance {
  instance_id: number
  /** Nullable on purpose: an abstention is usable, a wrong label is not. */
  fdi: number | null
  fdi_confidence?: number | null
  centroid?: [number, number, number]
  crown_area_mm2?: number | null
}

export interface Segmentation {
  arch: 'upper' | 'lower' | 'both'
  unassigned_region_pct: number
  instances: SegmentationInstance[]
  flags: string[]
  /** `volumetric` for a multi-label mask over a CT. Absent on the FDI path. */
  kind?: 'volumetric' | 'fdi'
  classes?: { id: number; name: string; volume?: number }[]
  mask_ref?: string
  mask_header_ref?: string
}

/** Companion written alongside a per-vertex label blob. */
export interface VertexLabelHeader {
  kind: 'vertex'
  count: number
  legend: Record<string, string>
  counts: Record<string, number>
}

export interface Measurement {
  name: string
  value: number
  unit: 'mm' | 'um' | 'pct' | 'deg'
  uncertainty?: number
  method?: string
}

export interface ResultEnvelope {
  schema_version: string
  result_id: string
  source_capture_id: string
  /** Operations that ran, in order. */
  ops: string[]
  model_version: string
  scale: {
    verified: boolean
    source: 'depth_sensor' | 'fiducial' | 'prior_scan' | 'pixel_spacing' | 'unknown'
    scale_error_pct?: number
  }
  rectification?: {
    frames_in: number
    frames_kept: number
    depth_frames: number
    depth_aligned: boolean
    extrinsics_source: string | null
  } | null
  geometry?: {
    voxel_size_mm?: number
    frames_used?: number
    registration_residual_mm?: number
    /** Density threshold an extracted surface was taken at, in the volume's units. */
    threshold?: number
    stride?: number
    /** [xmin, xmax, ymin, ymax, zmin, zmax] of an extracted surface, world space. */
    bounds?: number[]
    /** Labels present on a labelled surface, in ascending order. */
    labels?: number[]
  } | null
  artifacts: ResultArtifact[]
  segmentation?: Segmentation | null
  measurements: Measurement[]
  quality?: {
    gate_passed: boolean
    rejections: { code: string; detail?: string; frame_ids?: number[] }[]
    coverage_pct?: number
  }
  warnings: string[]
  provenance: {
    backend: string
    started_at?: string
    duration_ms?: number
    [key: string]: unknown
  }
}

export interface Job {
  job_id: string
  status: JobStatus
  ops: string[]
  backend: string | null
  created_at: string
  finished_at: string | null
  result: ResultEnvelope | null
  error: string | null
  warnings: string[]
}

export interface JobCreate {
  stages: Stage[]
  capture?: Record<string, unknown> | null
  backend?: string | null
  allow_diagnostic?: boolean
}

export type SourceKind = 'dicom' | 'images'

export interface SourceSummary {
  source_id: string
  kind: SourceKind
  name: string
  /** Name as discovered at upload, kept so a rename stays reversible. */
  original_name: string | null
  workspace_id: string
  set_id: string
  created_at: string
  modality: string | null
  instance_count: number
  bytes_total: number
  headerless: boolean
  renderable: boolean
  /** Present when renderable is false: why there is nothing to draw. */
  render_reason: string | null
}

export interface VolumeHeader {
  dims: [number, number, number]
  spacing: [number, number, number]
  origin: [number, number, number]
  byte_length: number
  value_range: [number, number]
  window_center: number | null
  window_width: number | null
}

export interface VolumePayload {
  header_ref: string
  bin_ref: string
  header: VolumeHeader
}
