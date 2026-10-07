import { useState } from 'react'

import type { SourceSummary, VolumePayload } from '../lib/types'
import { ImageView } from './ImageView'
import type { ResultView } from './ResultsSection'
import { Viewport3D } from './Viewport3D'

const TABS = [
  { id: '3d', label: '3D View' },
  { id: 'images', label: 'Image View' },
] as const

type TabId = (typeof TABS)[number]['id']

/**
 * The main panel: the geometry on one tab, the pixels it came from on the other.
 *
 * Both are views of the same source rather than two screens, so they share the
 * source selection above and the results below — switching tabs is changing
 * your mind about how to look, not about what you are looking at.
 */
export function MainPanel({
  source,
  volume,
  results,
  busy,
}: {
  source: SourceSummary | null
  volume: VolumePayload | null
  results: ResultView[]
  busy: boolean
}) {
  const [tab, setTab] = useState<TabId>('3d')

  return (
    <section className="viewport">
      <div className="view-tabs" role="tablist">
        {TABS.map((entry) => (
          <button
            key={entry.id}
            role="tab"
            aria-selected={tab === entry.id}
            className={`view-tab${tab === entry.id ? ' active' : ''}`}
            onClick={() => setTab(entry.id)}
          >
            {entry.label}
          </button>
        ))}
      </div>

      {tab === '3d' ? (
        <Viewport3D source={source} volume={volume} results={results} busy={busy} />
      ) : (
        <div className="view-pane">
          <ImageView source={source} />
        </div>
      )}
    </section>
  )
}
