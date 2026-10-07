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
  /** Camera poses, from the device tracker or solved from the images. */
  ESTIMATE_POSES: 'estimate_poses',
  /** Teeth from a single panoramic radiograph. */
  PX2TOOTH: 'px2tooth',
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

export interface SegmenterClasses {
  task: string
  /** False when there is no usable segmenter at all, which is a different
   *  problem from one that runs but cannot name what it finds. */
  available: boolean
  detail: string | null
  classes: { id: number; name: string }[]
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
    /** Cell size the surface was simplified at, in millimetres. */
    simplify_mm?: number
    /** Counts before simplifying, so the reduction can be stated. */
    vertices_before?: number
    triangles_before?: number
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
    /** What ran at each stage, recorded so a result can be traced back. */
    stages?: {
      op: string
      params?: Record<string, unknown> | null
      tool?: string
      /** One sentence on what the step does, shown on hover rather than in the manifest. */
      description?: string
      algorithm?: string
      notes?: string
      duration_ms?: number
    }[]
    [key: string]: unknown
  }
}

export interface Job {
  job_id: string
  status: JobStatus
  ops: string[]
  backend: string | null
  /** What the operator calls this result. Null means the derived label. */
  name?: string | null
  source_id?: string | null
  created_at: string
  finished_at: string | null
  result: ResultEnvelope | null
  error: string | null
  warnings: string[]
}

export interface JobCreate {
  stages: Stage[]
  /** Which workflow this is being run as, recorded on the job. */
  workflow?: string
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

/** A dataset the app can pull in from a URL. */
export interface SampleEntry {
  id: string
  title: string
  description: string
  url: string
  /** Shown beside the download, so the terms travel with the data. */
  licence: string
  size_mb: number | null
  /** Exact size, so the display does not round a small study to "0 MB". */
  bytes: number | null
  kind: string
  modality: string | null
  anatomy: string | null
  /**
   * Who it came from and how to cite it.
   *
   * These are not decoration: a CC BY licence permits commercial use *with
   * attribution*, so showing the licence without these does not satisfy it.
   */
  collection: string | null
  source: string | null
  doi: string | null
  notes: string | null
  tags: string[]
  /** False when this deployment has no bucket configured for it. */
  available: boolean
}

export interface SamplesCatalogue {
  configured: boolean
  base: string | null
  samples: SampleEntry[]
  /** Modalities present in the catalogue, for filtering. */
  modalities: string[]
}

/** A source, plus where it came from when it was fetched rather than uploaded. */
export interface ImportedSource extends SourceSummary {
  imported_from?: string
  file_count?: number
  /** What the contents looked like. */
  detected?: SourceKind
  detected_reason?: string
  /** False when the archive was mixed or unrecognised. */
  detected_confident?: boolean
  /** What was actually created, which differs when detection was overridden. */
  kind_used?: SourceKind
  kind_overridden?: boolean
}

/** What a camera-pose solver will accept. A sequence is not an unordered set. */
export type InputShape = 'unordered_images' | 'image_sequence' | 'device_poses'

/** One framework that can work out where the camera was. */
export interface SolverEntry {
  name: string
  tool: string
  description: string
  algorithm: string
  accepts: InputShape[]
  /** Carried because one of them is copyleft; see pipeline/solvers.py. */
  licence: string
  reference: string | null
  notes: string | null
  /** False when this deployment cannot run it — do not offer it. */
  available: boolean
  detail: string | null
}

export interface SolversResponse {
  solvers: SolverEntry[]
  available: string[]
  shapes: { name: InputShape; description: string }[]
}

/** One entry in the Image View: a DICOM slice, or an uploaded photo. */
export interface SourceImage {
  index: number
  name: string
  /** Photos only: the original size on disk. */
  bytes?: number
  media_type?: string
}

export interface SourceImages {
  source_id: string
  kind: SourceKind
  /** Present for DICOM: the plane the slices are cut on. */
  axis?: string
  count: number
  /** DICOM only: the series window, and the spacing between listed slices. */
  window?: { center: number | null; width: number | null }
  slice_spacing_mm?: number | null
  images: SourceImage[]
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
