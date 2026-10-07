/**
 * Editor state: workspaces, working sets, and the sources loaded into them.
 *
 * Workspace      top-level container — a clinic, a project
 * Working set    a named collection of sources within a workspace
 * Source         one dropped item — a DICOM series, or a set of images
 *
 * Structure (names and ids) persists to localStorage so a reload lands back in
 * the same place. Source items do not: their payloads are storage refs that go
 * stale across restarts, and rehydrating a broken reference would be worse than
 * starting empty.
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

export type SourceKind = 'dicom' | 'images'
export type SourceStatus = 'ready' | 'busy' | 'error'

export interface Source {
  id: string
  kind: SourceKind
  name: string
  /** Short descriptor: "412 slices", "24 images". */
  meta: string
  status: SourceStatus
  error?: string
  /** DICOM series id, or the file list for an image set. */
  payload?: unknown
}

export interface WorkingSet {
  id: string
  name: string
}

export interface Workspace {
  id: string
  name: string
  sets: WorkingSet[]
}

interface EditorState {
  workspaces: Workspace[]
  activeWorkspaceId: string
  activeSetId: string
  sources: Record<string, Source[]>
  activeSourceId: string | null
}

interface EditorContextValue extends EditorState {
  activeWorkspace: Workspace
  activeSet: WorkingSet
  activeSources: Source[]
  activeSource: Source | null

  selectWorkspace: (id: string) => void
  selectSet: (id: string) => void
  selectSource: (id: string | null) => void

  addWorkspace: (name: string) => void
  addSet: (name: string) => void
  renameSet: (id: string, name: string) => void

  addSources: (sources: Source[]) => void
  updateSource: (id: string, patch: Partial<Source>) => void
  clearSources: () => void
}

const STORAGE_KEY = 'bone-viewer.editor.structure.v1'

const newId = () => crypto.randomUUID()

function seed(): EditorState {
  const workspaceId = newId()
  const setId = newId()
  return {
    workspaces: [{ id: workspaceId, name: 'Workspace 1', sets: [{ id: setId, name: 'Working set 1' }] }],
    activeWorkspaceId: workspaceId,
    activeSetId: setId,
    sources: {},
    activeSourceId: null,
  }
}

/** Names and ids only; see the module docstring for why items are excluded. */
function loadStructure(): Pick<EditorState, 'workspaces' | 'activeWorkspaceId' | 'activeSetId'> | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed.workspaces) || parsed.workspaces.length === 0) return null
    return parsed
  } catch {
    return null
  }
}

const EditorContext = createContext<EditorContextValue | null>(null)

export function EditorProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<EditorState>(() => {
    const restored = loadStructure()
    return restored ? { ...seed(), ...restored, sources: {}, activeSourceId: null } : seed()
  })

  useEffect(() => {
    const { workspaces, activeWorkspaceId, activeSetId } = state
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ workspaces, activeWorkspaceId, activeSetId }))
    } catch {
      // Storage full or blocked; structure simply will not persist.
    }
  }, [state])

  const activeWorkspace =
    state.workspaces.find((w) => w.id === state.activeWorkspaceId) ?? state.workspaces[0]
  const activeSet =
    activeWorkspace.sets.find((s) => s.id === state.activeSetId) ?? activeWorkspace.sets[0]
  const activeSources = state.sources[activeSet.id] ?? []
  const activeSource = activeSources.find((s) => s.id === state.activeSourceId) ?? null

  const addSources = useCallback(
    (incoming: Source[]) => {
      setState((prev) => {
        const setId = prev.activeSetId
        const existing = prev.sources[setId] ?? []
        return {
          ...prev,
          sources: { ...prev.sources, [setId]: [...existing, ...incoming] },
          activeSourceId: incoming[0]?.id ?? prev.activeSourceId,
        }
      })
    },
    [],
  )

  const updateSource = useCallback((id: string, patch: Partial<Source>) => {
    setState((prev) => {
      const setId = prev.activeSetId
      const existing = prev.sources[setId] ?? []
      return {
        ...prev,
        sources: {
          ...prev.sources,
          [setId]: existing.map((s) => (s.id === id ? { ...s, ...patch } : s)),
        },
      }
    })
  }, [])

  const value = useMemo<EditorContextValue>(() => {
    return {
      ...state,
      activeWorkspace,
      activeSet,
      activeSources,
      activeSource,

      selectWorkspace: (id) =>
        setState((prev) => {
          const workspace = prev.workspaces.find((w) => w.id === id)
          return {
            ...prev,
            activeWorkspaceId: id,
            activeSetId: workspace?.sets[0]?.id ?? prev.activeSetId,
            activeSourceId: null,
          }
        }),

      selectSet: (id) => setState((prev) => ({ ...prev, activeSetId: id, activeSourceId: null })),

      selectSource: (id) => setState((prev) => ({ ...prev, activeSourceId: id })),

      addWorkspace: (name) =>
        setState((prev) => {
          const workspace: Workspace = { id: newId(), name, sets: [{ id: newId(), name: 'Working set 1' }] }
          return {
            ...prev,
            workspaces: [...prev.workspaces, workspace],
            activeWorkspaceId: workspace.id,
            activeSetId: workspace.sets[0].id,
            activeSourceId: null,
          }
        }),

      addSet: (name) =>
        setState((prev) => {
          const set: WorkingSet = { id: newId(), name }
          return {
            ...prev,
            workspaces: prev.workspaces.map((w) =>
              w.id === prev.activeWorkspaceId ? { ...w, sets: [...w.sets, set] } : w,
            ),
            activeSetId: set.id,
            activeSourceId: null,
          }
        }),

      renameSet: (id, name) =>
        setState((prev) => ({
          ...prev,
          workspaces: prev.workspaces.map((w) =>
            w.id === prev.activeWorkspaceId
              ? { ...w, sets: w.sets.map((s) => (s.id === id ? { ...s, name } : s)) }
              : w,
          ),
        })),

      addSources,
      updateSource,
      clearSources: () =>
        setState((prev) => ({
          ...prev,
          sources: { ...prev.sources, [prev.activeSetId]: [] },
          activeSourceId: null,
        })),
    }
  }, [state, activeWorkspace, activeSet, activeSources, activeSource, addSources, updateSource])

  return <EditorContext.Provider value={value}>{children}</EditorContext.Provider>
}

export function useEditor(): EditorContextValue {
  const ctx = useContext(EditorContext)
  if (!ctx) throw new Error('useEditor must be used inside <EditorProvider>')
  return ctx
}
