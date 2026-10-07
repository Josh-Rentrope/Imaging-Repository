/**
 * API client.
 *
 * Everything goes through `/api`, which Vite proxies to the backend in dev, so
 * the browser is same-origin and there is no environment-specific URL handling
 * in components.
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
  SourceSummary,
  VolumePayload,
} from './types'

const BASE = '/api'

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
    response = await fetch(`${BASE}${path}`, init)
  } catch (cause) {
    throw new ApiError(
      `Cannot reach the API at ${BASE}. Start it with: cd apps/api && uv run uvicorn app.main:app --reload --port 8787`,
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
  return `${BASE}/artifacts/${key}`
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
    const response = await fetch(`${BASE}/exports`, {
      method: 'POST',
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
