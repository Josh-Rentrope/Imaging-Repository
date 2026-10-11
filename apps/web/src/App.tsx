import { BrowserRouter, Route, Routes } from 'react-router'

import { AppError } from './components/AppError'
import { AppShell } from './layouts/AppShell'
import CaptureRoute from './routes/CaptureRoute'
import EditorRoute from './routes/EditorRoute'
import NotFoundRoute from './routes/NotFoundRoute'
import { EditorProvider } from './state/editor'
import { PageTransition, TransitionProvider } from './transitions'

/**
 * One editor screen, plus a capture screen.
 *
 * `/capture` is matched before the desktop shell is built, so a phone never
 * mounts the workspace switcher, the viewport or the result panel -- and never
 * issues the fetches that fill them. It stays inside `EditorProvider` because a
 * capture has to land in the working set the editor is showing, and that is
 * where those ids live.
 *
 * The shell is a component rather than inline so the desktop keeps exactly the
 * tree it had; adding a route should not rearrange the one that was working.
 */
export default function App() {
  return (
    // Outermost, so a failure anywhere below -- including in the router or the
    // providers that seed their own state -- says what happened rather than
    // leaving an empty page.
    <AppError>
      <BrowserRouter>
        <EditorProvider>
          <Routes>
            <Route path="/capture" element={<CaptureRoute />} />
            <Route path="*" element={<Desktop />} />
          </Routes>
        </EditorProvider>
      </BrowserRouter>
    </AppError>
  )
}

function Desktop() {
  return (
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
  )
}
