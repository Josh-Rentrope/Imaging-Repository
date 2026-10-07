/**
 * API client.
 *
 * Everything goes through `/api`, which Vite proxies to the backend in dev (see
 * vite.config.ts). Same-origin in both dev and prod means no CORS preflight and
 * no environment-specific URL juggling in components.
 */

import type {
  CapabilitiesResponse,
  DicomSeries,
  DicomSeriesSummary,
  HealthBackend,
  Job,
  JobCreate,
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
      `Cannot reach the API at ${BASE}. Is it running? Start it with: cd apps/api && uv run uvicorn app.main:app --reload --port 8787`,
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

/** Turn an opaque storage ref into something an <img>/loader can fetch. */
export function artifactUrl(ref: string): string {
  const key = ref.includes('://') ? ref.split('://', 2)[1] : ref
  return `${BASE}/artifacts/${key}`
}

export const api = {
  health: () => request<{ ok: boolean; backends: HealthBackend[] }>('/health'),
  capabilities: () => request<CapabilitiesResponse>('/capabilities'),

  listJobs: (limit = 50) => request<Job[]>(`/jobs?limit=${limit}`),
  getJob: (id: string) => request<Job>(`/jobs/${id}`),
  submitJob: (body: JobCreate) =>
    request<Job>('/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),

  listSeries: () => request<DicomSeriesSummary[]>('/dicom/series'),
  getSeries: (id: string) => request<DicomSeries>(`/dicom/series/${encodeURIComponent(id)}`),

  uploadDicom: async (files: File[]): Promise<DicomSeries[]> => {
    const form = new FormData()
    for (const file of files) form.append('files', file, file.name)
    return request<DicomSeries[]>('/dicom/series', { method: 'POST', body: form })
  },
}

export async function fetchArtifactText(ref: string): Promise<string> {
  const response = await fetch(artifactUrl(ref))
  if (!response.ok) {
    throw new ApiError(`Failed to fetch artefact (${response.status})`, response.status)
  }
  return response.text()
}
