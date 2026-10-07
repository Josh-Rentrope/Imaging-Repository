import type { Job } from '../lib/types'

function Measurements({ job }: { job: Job }) {
  const result = job.result
  if (!result) return null

  if (!result.scale.verified) {
    return <p className="muted" style={{ margin: 0 }}>None — no metric anchor.</p>
  }
  if (result.measurements.length === 0) {
    return <p className="muted" style={{ margin: 0 }}>None produced.</p>
  }

  return (
    <dl className="inspector">
      {result.measurements.map((m) => (
        <div key={m.name} style={{ display: 'contents' }}>
          <dt>{m.name}</dt>
          <dd>
            {m.value} {m.unit}
            {m.uncertainty != null && <span className="muted"> ± {m.uncertainty}</span>}
          </dd>
        </div>
      ))}
    </dl>
  )
}

function Segmentation({ job }: { job: Job }) {
  const seg = job.result?.segmentation
  if (!seg) return <p className="muted" style={{ margin: 0 }}>Not requested.</p>

  const abstentions = seg.instances.filter((i) => i.fdi === null).length

  return (
    <>
      <dl className="inspector">
        <div style={{ display: 'contents' }}>
          <dt>arch</dt>
          <dd>{seg.arch}</dd>
        </div>
        <div style={{ display: 'contents' }}>
          <dt>teeth</dt>
          <dd>{seg.instances.length - abstentions} labelled</dd>
        </div>
        {abstentions > 0 && (
          <div style={{ display: 'contents' }}>
            <dt>abstained</dt>
            <dd className="muted">{abstentions}</dd>
          </div>
        )}
        <div style={{ display: 'contents' }}>
          <dt>unassigned</dt>
          <dd className="muted">{seg.unassigned_region_pct}%</dd>
        </div>
      </dl>
      {seg.flags.length > 0 && (
        <div className="controls">
          {seg.flags.map((flag) => (
            <span key={flag} className="tag">
              {flag}
            </span>
          ))}
        </div>
      )}
    </>
  )
}

export function ResultSection({ job }: { job: Job | null }) {
  if (!job) return null

  if (job.status === 'failed' || job.status === 'rejected') {
    return (
      <div className="section">
        <h2 className="section-title">Result</h2>
        <p className="error-text" style={{ margin: 0 }}>
          {job.error}
        </p>
      </div>
    )
  }

  if (!job.result) return null
  const result = job.result

  return (
    <div className="section">
      <h2 className="section-title">Result</h2>

      <details open>
        <summary>Measurements</summary>
        <Measurements job={job} />
      </details>

      <details style={{ marginTop: 8 }}>
        <summary>Segmentation</summary>
        <Segmentation job={job} />
      </details>

      {result.warnings.length > 0 && (
        <details style={{ marginTop: 8 }}>
          <summary>Warnings ({result.warnings.length})</summary>
          <ul style={{ margin: '6px 0 0', paddingLeft: 16 }}>
            {result.warnings.map((warning) => (
              <li key={warning} className="muted" style={{ fontSize: 12 }}>
                {warning}
              </li>
            ))}
          </ul>
        </details>
      )}

      <details style={{ marginTop: 8 }}>
        <summary>Envelope</summary>
        <pre>{JSON.stringify(result, null, 2)}</pre>
      </details>
    </div>
  )
}
