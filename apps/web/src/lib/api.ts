/**
 * API client.
 *
 * Two topologies, one place that knows about them.
 *
 * In development the app is served by Vite and `/api` is proxied to the backend,
 * so the browser is same-origin. Deployed, the API is a separate origin, so the
 * base has to be supplied at build time as `VITE_API_BASE`; leaving it unset
 * keeps the relative default and nothing changes locally.
 *
 * Everything that decides where the API lives -- the base, whether a request
 * carries credentials -- lives here, so a component cannot get it subtly wrong.
 * It has been got wrong before: `ImageView` built its own `/api/...` string, and
 * a change to `BASE` alone would have left every DICOM thumbnail 404ing.
 */

import type {
  CapabilitiesResponse,
  HealthBackend,
  Job,
  JobCreate,
  SegmenterClasses,
  ImportedSource,
  SamplesCatalogue,
  SolversResponse,
  SourceImages,
  SourceKind,
  SourceSummary,
  VolumePayload,
} from './types'

/**
 * Where the API is. Trailing slashes are trimmed so a base of
 * `https://api.example.com/` does not produce `//health`.
 */
export const API_BASE = (import.meta.env.VITE_API_BASE ?? '/api').replace(/\/+$/, '')

/**
 * Requests carry the viewer cookie.
 *
 * The cookie identifies a viewer, and a viewer cannot be read without it, so a
 * cross-origin read that omits it is answered with somebody else's data being
 * invisible -- or, for an artifact ref, a 404. Same-origin this is a no-op.
 */
const CREDENTIALS: RequestCredentials = 'include'

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly detail?: unknown,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`, { credentials: CREDENTIALS, ...init })
  } catch (cause) {
    throw new ApiError(
      `Cannot reach the API at ${API_BASE}. Start it with: cd apps/api && uv run uvicorn app.main:app --reload --port 8787`,
      0,
      cause,
    )
  }

  if (!response.ok) {
    let detail: unknown
    try {
      detail = await response.json()
    } catch {
      detail = await response.text().catch(() => undefined)
    }
    const message =
      typeof detail === 'object' && detail !== null && 'detail' in detail
        ? String((detail as { detail: unknown }).detail)
        : `${response.status} ${response.statusText}`
    throw new ApiError(message, response.status, detail)
  }

  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

/** Turn an opaque storage ref into something fetchable. */
export function artifactUrl(ref: string): string {
  const key = ref.includes('://') ? ref.split('://', 2)[1] : ref
  return `${API_BASE}/artifacts/${key}`
}

/**
 * Read an artifact.
 *
 * A mesh or a volume is still a read against the API, so it carries the viewer
 * cookie like every other call. A bare `fetch` would omit it cross-origin, which
 * would not look like a permissions problem — it would look like the artifact
 * having been deleted.
 */
export function fetchArtifact(url: string): Promise<Response> {
  return fetch(url, { credentials: CREDENTIALS })
}

function uploadForm(files: File[], workspaceId: string, setId: string): FormData {
  const form = new FormData()
  for (const file of files) form.append('files', file, file.name)
  form.append('workspace_id', workspaceId)
  form.append('set_id', setId)
  return form
}

export const api = {
  health: () => request<{ ok: boolean; backends: HealthBackend[] }>('/health'),
  capabilities: () => request<CapabilitiesResponse>('/capabilities'),

  listJobs: (limit = 50, sourceId?: string) =>
    request<Job[]>(
      `/jobs?limit=${limit}${sourceId ? `&source_id=${encodeURIComponent(sourceId)}` : ''}`,
    ),
  getJob: (id: string) => request<Job>(`/jobs/${id}`),

  /** Name a result. Stored on the job, so it survives a reload. */
  renameJob: (id: string, name: string | null) =>
    request<Job>(`/jobs/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    }),

  /** Remove a result and the geometry it produced. */
  deleteJob: (id: string) =>
    request<void>(`/jobs/${id}`, { method: 'DELETE' }),

  /** What a segmenter can find. Answered without running it. */
  segmenterClasses: (task = 'total') =>
    request<SegmenterClasses>(`/segmenter/classes?task=${encodeURIComponent(task)}`),

  /** Datasets this deployment offers to pull in. */
  listSamples: () => request<SamplesCatalogue>('/samples'),

  /** Camera-pose solvers, and which of them can actually run here. */
  listSolvers: () => request<SolversResponse>('/solvers'),

  /**
   * Download a dataset from a URL and add it as a source.
   *
   * The server does the fetching and unpacking, so this is one request rather
   * than a download-and-reupload from the browser.
   */
  importRemote: (body: {
    url: string
    workspace_id: string
    set_id: string
    name?: string
    /** Omit to let the server decide from the contents. */
    kind?: SourceKind
  }) =>
    request<ImportedSource>('/sources/remote', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),

  /** What the Image View can show for a source. */
  listImages: (sourceId: string) => request<SourceImages>(`/sources/${sourceId}/images`),

  /**
   * Selected results as a zip.
   *
   * Returns the body rather than going through `request`, which parses JSON —
   * this is an archive and reading it as text would corrupt it.
   */
  exportResults: async (body: {
    result_ids: string[]
    format: string
    include_labels: boolean
  }): Promise<Blob> => {
    const response = await fetch(`${API_BASE}/exports`, {
      method: 'POST',
      credentials: CREDENTIALS,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`
      try {
        detail = (await response.json()).detail ?? detail
      } catch {
        // A non-JSON error body is not worth a second failure.
      }
      throw new Error(detail)
    }
    return response.blob()
  },
  submitJob: (body: JobCreate) =>
    request<Job>('/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),

  listSources: (workspaceId: string, setId: string) =>
    request<SourceSummary[]>(
      `/sources?workspace_id=${encodeURIComponent(workspaceId)}&set_id=${encodeURIComponent(setId)}`,
    ),

  uploadDicom: (files: File[], workspaceId: string, setId: string) =>
    request<SourceSummary>('/sources/dicom', {
      method: 'POST',
      body: uploadForm(files, workspaceId, setId),
    }),

  uploadImages: (files: File[], workspaceId: string, setId: string) =>
    request<SourceSummary>('/sources/images', {
      method: 'POST',
      body: uploadForm(files, workspaceId, setId),
    }),

  renameSource: (id: string, name: string) => {
    const form = new FormData()
    form.append('name', name)
    return request<SourceSummary>(`/sources/${id}`, { method: 'PATCH', body: form })
  },

  deleteSource: (id: string) => request<void>(`/sources/${id}`, { method: 'DELETE' }),

  getVolume: (id: string) => request<VolumePayload>(`/sources/${id}/volume`),
}
