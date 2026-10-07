import { Link, useLocation } from 'react-router'

export default function NotFoundRoute() {
  const location = useLocation()

  return (
    <div className="empty-state">
      <span>
        No route matches <code>{location.pathname}</code>
      </span>
      <Link to="/">Back to the editor</Link>
    </div>
  )
}
