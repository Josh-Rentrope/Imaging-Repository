/**
 * Editor state.
 *
 * Workspace      top-level container — a clinic, a project
 * Working set    a named collection of sources within a workspace
 * Source         one uploaded DICOM series or image set
 *
 * Workspaces and working sets are client-side and persist to localStorage;
 * sources are server-side and are re-fetched whenever the active set changes.
 * That split is deliberate: the server is the only thing that can hold the
 * uploads themselves, so the client must not pretend to own them.
 *
 * The workspace and set ids are opaque strings sent to the server as scoping
 * tags. They become server-issued once there is a tenant model.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'

import { api } from '../lib/api'
import type { SourceSummary } from '../lib/types'

export interface WorkingSet {
  id: string
  name: string
}

export interface Workspace {
  id: string
  name: string
  sets: WorkingSet[]
}

interface Structure {
  workspaces: Workspace[]
  activeWorkspaceId: string
  activeSetId: string
}

interface EditorContextValue extends Structure {
  activeWorkspace: Workspace
  activeSet: WorkingSet

  sources: SourceSummary[]
  sourcesLoading: boolean
  sourcesError: string | null
  activeSourceId: string | null
  activeSource: SourceSummary | null

  selectWorkspace: (id: string) => void
  selectSet: (id: string) => void
  selectSource: (id: string | null) => void

  addWorkspace: (name: string) => void
  renameWorkspace: (id: string, name: string) => void
  deleteWorkspace: (id: string) => void

  addSet: (name: string) => void
  renameSet: (id: string, name: string) => void
  deleteSet: (id: string) => void

  refreshSources: () => Promise<void>
  renameSource: (id: string, name: string) => Promise<void>
  deleteSource: (id: string) => Promise<void>
}

const STORAGE_KEY = 'bone-viewer.editor.structure.v2'

const newId = () => crypto.randomUUID()

function seed(): Structure {
  const workspaceId = newId()
  const setId = newId()
  return {
    workspaces: [
      { id: workspaceId, name: 'Workspace 1', sets: [{ id: setId, name: 'Working set 1' }] },
    ],
    activeWorkspaceId: workspaceId,
    activeSetId: setId,
  }
}

function load(): Structure {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return seed()
    const parsed = JSON.parse(raw) as Structure
    if (!Array.isArray(parsed.workspaces) || parsed.workspaces.length === 0) return seed()
    if (!parsed.workspaces.some((w) => w.id === parsed.activeWorkspaceId)) return seed()
    return parsed
  } catch {
    return seed()
  }
}

const EditorContext = createContext<EditorContextValue | null>(null)

export function EditorProvider({ children }: { children: ReactNode }) {
  const [structure, setStructure] = useState<Structure>(load)
  const [sources, setSources] = useState<SourceSummary[]>([])
  const [sourcesLoading, setSourcesLoading] = useState(false)
  const [sourcesError, setSourcesError] = useState<string | null>(null)
  const [activeSourceId, setActiveSourceId] = useState<string | null>(null)

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(structure))
    } catch {
      // Storage unavailable or full; the structure simply will not persist.
    }
  }, [structure])

  const activeWorkspace =
    structure.workspaces.find((w) => w.id === structure.activeWorkspaceId) ?? structure.workspaces[0]
  const activeSet =
    activeWorkspace.sets.find((s) => s.id === structure.activeSetId) ?? activeWorkspace.sets[0]

  const refreshSources = useCallback(async () => {
    setSourcesLoading(true)
    setSourcesError(null)
    try {
      const list = await api.listSources(activeWorkspace.id, activeSet.id)
      setSources(list)
      setActiveSourceId((current) =>
        current && list.some((s) => s.source_id === current) ? current : (list[0]?.source_id ?? null),
      )
    } catch (cause) {
      setSourcesError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setSourcesLoading(false)
    }
  }, [activeWorkspace.id, activeSet.id])

  // Re-fetch whenever the working set changes. This is what makes uploads
  // survive a reload: the server held them all along.
  useEffect(() => {
    void refreshSources()
  }, [refreshSources])

  const mutate = useCallback((update: (structure: Structure) => Structure) => {
    setStructure((current) => update(current))
  }, [])

  const value = useMemo<EditorContextValue>(() => {
    const activeSource = sources.find((s) => s.source_id === activeSourceId) ?? null

    return {
      ...structure,
      activeWorkspace,
      activeSet,
      sources,
      sourcesLoading,
      sourcesError,
      activeSourceId,
      activeSource,

      selectWorkspace: (id) =>
        mutate((current) => {
          const workspace = current.workspaces.find((w) => w.id === id)
          return {
            ...current,
            activeWorkspaceId: id,
            activeSetId: workspace?.sets[0]?.id ?? current.activeSetId,
          }
        }),
      selectSet: (id) => mutate((current) => ({ ...current, activeSetId: id })),
      selectSource: setActiveSourceId,

      addWorkspace: (name) =>
        mutate((current) => {
          const workspace: Workspace = { id: newId(), name, sets: [{ id: newId(), name: 'Working set 1' }] }
          return {
            ...current,
            workspaces: [...current.workspaces, workspace],
            activeWorkspaceId: workspace.id,
            activeSetId: workspace.sets[0].id,
          }
        }),

      renameWorkspace: (id, name) =>
        mutate((current) => ({
          ...current,
          workspaces: current.workspaces.map((w) => (w.id === id ? { ...w, name } : w)),
        })),

      deleteWorkspace: (id) =>
        mutate((current) => {
          if (current.workspaces.length <= 1) return current
          const workspaces = current.workspaces.filter((w) => w.id !== id)
          const stillThere = workspaces.some((w) => w.id === current.activeWorkspaceId)
          const next = stillThere ? current.activeWorkspaceId : workspaces[0].id
          return {
            ...current,
            workspaces,
            activeWorkspaceId: next,
            activeSetId:
              workspaces.find((w) => w.id === next)?.sets[0]?.id ?? current.activeSetId,
          }
        }),

      addSet: (name) =>
        mutate((current) => {
          const set: WorkingSet = { id: newId(), name }
          return {
            ...current,
            workspaces: current.workspaces.map((w) =>
              w.id === current.activeWorkspaceId ? { ...w, sets: [...w.sets, set] } : w,
            ),
            activeSetId: set.id,
          }
        }),

      renameSet: (id, name) =>
        mutate((current) => ({
          ...current,
          workspaces: current.workspaces.map((w) =>
            w.id === current.activeWorkspaceId
              ? { ...w, sets: w.sets.map((s) => (s.id === id ? { ...s, name } : s)) }
              : w,
          ),
        })),

      deleteSet: (id) =>
        mutate((current) => {
          const workspace = current.workspaces.find((w) => w.id === current.activeWorkspaceId)
          if (!workspace || workspace.sets.length <= 1) return current
          const sets = workspace.sets.filter((s) => s.id !== id)
          return {
            ...current,
            workspaces: current.workspaces.map((w) =>
              w.id === current.activeWorkspaceId ? { ...w, sets } : w,
            ),
            activeSetId: sets.some((s) => s.id === current.activeSetId)
              ? current.activeSetId
              : sets[0].id,
          }
        }),

      refreshSources,

      renameSource: async (id, name) => {
        const updated = await api.renameSource(id, name)
        setSources((current) => current.map((s) => (s.source_id === id ? updated : s)))
      },

      deleteSource: async (id) => {
        await api.deleteSource(id)
        setSources((current) => current.filter((s) => s.source_id !== id))
        setActiveSourceId((current) => (current === id ? null : current))
      },
    }
  }, [structure, activeWorkspace, activeSet, sources, sourcesLoading, sourcesError, activeSourceId, mutate, refreshSources])

  return <EditorContext.Provider value={value}>{children}</EditorContext.Provider>
}

export function useEditor(): EditorContextValue {
  const ctx = useContext(EditorContext)
  if (!ctx) throw new Error('useEditor must be used inside <EditorProvider>')
  return ctx
}
