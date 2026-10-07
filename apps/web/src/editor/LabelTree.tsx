import { useMemo, useState } from 'react'

/**
 * Group key for a structure name.
 *
 * Segmenters name their classes systematically, so the grouping is derived from
 * the name rather than declared: an index suffix comes off first, then a
 * laterality one. `rib_left_2` and `rib_left_3` land under `rib_left`,
 * `kidney_right` under `kidney`, and `heart` stands alone because nothing was
 * stripped from it. This is a presentation detail only — the names sent to the
 * backend are always the full ones.
 */
const INDEX_SUFFIX = /_?[A-Za-z]?\d+$/
const LATERALITY_SUFFIX = /_(?:left|right)$/

export function groupOf(name: string): string {
  const withoutIndex = name.replace(INDEX_SUFFIX, '')
  const withoutSide = withoutIndex.replace(LATERALITY_SUFFIX, '')
  return withoutSide || name
}

interface Group {
  name: string
  items: string[]
}

function group(names: string[]): Group[] {
  const byGroup = new Map<string, string[]>()
  for (const name of names) {
    const key = groupOf(name)
    const existing = byGroup.get(key)
    if (existing) existing.push(name)
    else byGroup.set(key, [name])
  }
  return [...byGroup.entries()]
    .map(([name, items]) => ({ name, items }))
    .sort((a, b) => a.name.localeCompare(b.name))
}

function TriCheckbox({
  state,
  onChange,
  title,
}: {
  state: 'on' | 'off' | 'partial'
  onChange: (on: boolean) => void
  title: string
}) {
  return (
    <input
      type="checkbox"
      title={title}
      checked={state === 'on'}
      // Indeterminate is not a prop React can set declaratively; it has to be
      // assigned on the node. The braces keep the ref from returning a value,
      // which React 19 would take as a cleanup function.
      ref={(node) => {
        if (node) node.indeterminate = state === 'partial'
      }}
      onChange={(event) => onChange(event.target.checked)}
    />
  )
}

/**
 * Structures to extract, as a grouped, filterable list of checkboxes.
 *
 * `selected` is `null` for every structure and a list for an explicit choice,
 * including the empty list. That distinction is why the list opens fully
 * checked rather than fully clear: an untouched picker means everything, so
 * showing empty boxes would misrepresent what is about to run — while still
 * allowing nothing to be ticked, which is a state worth passing through.
 */
export function LabelTree({
  names,
  selected,
  onChange,
}: {
  names: string[]
  selected: string[] | null
  onChange: (names: string[] | null) => void
}) {
  const [query, setQuery] = useState('')
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({})

  const active = useMemo(
    () => (selected === null ? new Set(names) : new Set(selected)),
    [names, selected],
  )

  const groups = useMemo(() => {
    const needle = query.trim().toLowerCase()
    const matching = needle
      ? names.filter((name) => name.toLowerCase().includes(needle))
      : names
    return group(matching)
  }, [names, query])

  // Collapse back to the compact form whenever nothing is excluded, so an
  // untouched list does not send a hundred names over the wire.
  const commit = (next: Set<string>) =>
    onChange(next.size === names.length ? null : names.filter((name) => next.has(name)))

  const toggle = (items: string[], on: boolean) => {
    const next = new Set(active)
    for (const item of items) {
      if (on) next.add(item)
      else next.delete(item)
    }
    commit(next)
  }

  if (names.length === 0) {
    return (
      <p className="popover-hint" style={{ margin: '2px 0 6px' }}>
        Run a segmentation first — the structure names come from its mask.
      </p>
    )
  }

  return (
    <div className="label-tree">
      <div className="label-tree-head">
        <input
          className="text-input mono"
          type="text"
          placeholder="filter structures"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <span className="muted mono" style={{ whiteSpace: 'nowrap' }}>
          {active.size} / {names.length}
        </span>
      </div>

      <div className="label-tree-actions">
        <button className="ghost" onClick={() => onChange(null)} disabled={selected === null}>
          All
        </button>
        <button className="ghost" onClick={() => onChange([])} disabled={active.size === 0}>
          None
        </button>
        {/* Applies to what is on screen, so a filter turns into a selection
            instead of forcing the visible names to be ticked one by one. */}
        {query.trim() && (
          <button
            className="ghost"
            onClick={() => {
              const next = new Set(active)
              for (const group of groups) for (const item of group.items) next.add(item)
              commit(next)
            }}
          >
            + filter
          </button>
        )}
      </div>

      <div className="label-tree-body">
        {groups.map((entry) => {
          const on = entry.items.filter((item) => active.has(item)).length
          const state = on === 0 ? 'off' : on === entry.items.length ? 'on' : 'partial'
          // A group of one is just a row: a header over a single child is a
          // level of nesting that says nothing.
          const single = entry.items.length === 1
          const isCollapsed = collapsed[entry.name] ?? true

          return (
            <div key={entry.name} className="label-group">
              <div className="label-group-row">
                <TriCheckbox
                  state={state}
                  onChange={(next) => toggle(entry.items, next)}
                  title={`All of ${entry.name}`}
                />
                {single ? (
                  <span className="label-group-name mono">{entry.items[0]}</span>
                ) : (
                  <button
                    className="label-group-name ghost mono"
                    onClick={() =>
                      setCollapsed((current) => ({
                        ...current,
                        [entry.name]: !isCollapsed,
                      }))
                    }
                    title={isCollapsed ? 'Expand' : 'Collapse'}
                  >
                    {isCollapsed ? '▸' : '▾'} {entry.name}
                    <span className="muted"> ({entry.items.length})</span>
                  </button>
                )}
              </div>

              {!isCollapsed &&
                !single &&
                entry.items.map((item) => (
                    <label key={item} className="label-item">
                      <input
                        type="checkbox"
                        checked={active.has(item)}
                        onChange={(event) => toggle([item], event.target.checked)}
                      />
                      {/* The full name, not the group's: this is what is sent. */}
                      <span className="mono">{item}</span>
                    </label>
                  ))}
            </div>
          )
        })}
      </div>
    </div>
  )
}
