import { BrowserRouter, Route, Routes } from 'react-router'

import { AppShell } from './layouts/AppShell'
import EditorRoute from './routes/EditorRoute'
import NotFoundRoute from './routes/NotFoundRoute'
import { EditorProvider } from './state/editor'
import { PageTransition, TransitionProvider } from './transitions'

/**
 * One screen: the editor. Workspaces and working sets are switched in the
 * header rather than by routing, so moving between them never unmounts the
 * viewport.
 */
export default function App() {
  return (
    <BrowserRouter>
      <EditorProvider>
        <TransitionProvider>
          <AppShell>
            <PageTransition>
              {(location) => (
                <Routes location={location}>
                  <Route path="/" element={<EditorRoute />} />
                  <Route path="*" element={<NotFoundRoute />} />
                </Routes>
              )}
            </PageTransition>
          </AppShell>
        </TransitionProvider>
      </EditorProvider>
    </BrowserRouter>
  )
}
