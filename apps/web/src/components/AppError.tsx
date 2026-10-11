/**
 * What a failure looks like, instead of a blank page.
 *
 * Added for a crash that took the whole app down while `EditorProvider` seeded
 * its state -- `crypto.randomUUID` does not exist in an insecure context. React's
 * default is to unmount everything and leave nothing, so on a phone the symptom
 * was indistinguishable from the page not loading, and the only place the reason
 * existed was a desktop console nobody had open.
 *
 * It does not attempt to recover. A tree that failed during startup is not one to
 * hand back to its children, and a boundary that retries is a boundary that hides
 * a reproducible fault.
 */

import { Component, type ErrorInfo, type ReactNode } from 'react'

interface State {
  error: Error | null
}

export class AppError extends Component<{ children: ReactNode }, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Logged rather than reported. There is no error service, and adding one is a
    // larger decision than this boundary.
    console.error('the app failed to render:', error, info.componentStack)
  }

  render() {
    const { error } = this.state
    if (!error) return this.props.children

    return (
      <div className="app-error">
        <h1>This page could not start</h1>
        <pre className="app-error__message">{error.message}</pre>
        <p className="app-error__hint">
          Reached over <code>http://</code> on a network address rather than{' '}
          <code>localhost</code> or <code>https</code>? Some browser features are
          unavailable in that context and a page with no way to reach them cannot run.
        </p>
        <button className="app-error__reload" onClick={() => window.location.reload()}>
          Reload
        </button>
      </div>
    )
  }
}
