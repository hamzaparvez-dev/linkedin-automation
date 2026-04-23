import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { apiGet } from '../api'
import type { LeadRow, Paginated } from '../types'

function actionStatusClass(st: string | null | undefined): string {
  const s = (st || '').toLowerCase()
  if (s === 'ok') return 'action-ok'
  if (s === 'error') return 'action-error'
  if (s === 'skipped') return 'action-skipped'
  return ''
}

export function Automation() {
  const [page, setPage] = useState(1)
  const [actionType, setActionType] = useState('')
  const [accountId, setAccountId] = useState('')
  const [dryRun, setDryRun] = useState<string>('')
  const [data, setData] = useState<Paginated<LeadRow> | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const limit = 50

  const load = useCallback(async () => {
    setErr(null)
    try {
      const qs = new URLSearchParams({ page: String(page), limit: String(limit) })
      if (actionType.trim()) qs.set('action_type', actionType.trim())
      if (accountId.trim()) qs.set('account_id', accountId.trim())
      if (dryRun === '0' || dryRun === '1') qs.set('dry_run', dryRun)
      const res = await apiGet<Paginated<LeadRow>>(`/api/actions?${qs}`)
      setData(res)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    }
  }, [page, actionType, accountId, dryRun])

  useEffect(() => {
    void load()
  }, [load])

  const totalPages = data ? Math.max(1, Math.ceil(data.total / limit)) : 1

  return (
    <div className="stack">
      <div className="panel">
        <h2>Phantombuster activity</h2>
        <p className="muted">
          Rows from <code>action_log</code>: every connect / DM attempt (including dry-run and
          skips). <strong>Status</strong> colors: <span className="badge">ok</span> success,{' '}
          <span className="badge" style={{ background: 'rgba(30, 58, 138, 0.5)' }}>
            skipped
          </span>{' '}
          expected (validation / Phantombuster dedupe &quot;already processed&quot;),{' '}
          <span className="badge" style={{ background: 'rgba(127, 29, 29, 0.4)' }}>error</span>{' '}
          failed. <Link to="/leads">Import leads (CSV)</Link> on the Leads page.
        </p>
        <div className="filters">
          <input
            className="input narrow"
            placeholder="action_type (connect | dm)"
            value={actionType}
            onChange={(e) => {
              setActionType(e.target.value)
              setPage(1)
            }}
          />
          <input
            className="input narrow"
            placeholder="account_id"
            value={accountId}
            onChange={(e) => {
              setAccountId(e.target.value)
              setPage(1)
            }}
          />
          <select
            className="input narrow"
            value={dryRun}
            onChange={(e) => {
              setDryRun(e.target.value)
              setPage(1)
            }}
          >
            <option value="">dry_run: any</option>
            <option value="1">dry_run: yes</option>
            <option value="0">dry_run: no</option>
          </select>
          <button type="button" className="btn" onClick={() => void load()}>
            Refresh
          </button>
        </div>
        {err && <p className="error-text">{err}</p>}
        {data && (
          <p className="muted">
            Page {data.page} of {totalPages} — {data.total} actions
          </p>
        )}
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Type</th>
                <th>Status</th>
                <th>Dry</th>
                <th>Account</th>
                <th>Lead</th>
                <th>Detail</th>
                <th>Phantombuster result / log</th>
                <th>Note preview</th>
              </tr>
            </thead>
            <tbody>
              {data?.items.map((row) => (
                <tr key={String(row.id)} className={actionStatusClass(row.status as string | undefined)}>
                  <td className="mono small">{String(row.created_at || '')}</td>
                  <td>
                    <span className="badge">{String(row.action_type || '')}</span>
                  </td>
                  <td>{String(row.status || '')}</td>
                  <td>{row.dry_run ? 'yes' : 'no'}</td>
                  <td>{String(row.account_id || '')}</td>
                  <td className="mono small">{String(row.lead_id || '').slice(0, 18)}…</td>
                  <td>{String(row.detail || '')}</td>
                  <td
                    className="clip mono small"
                    title={String(row.phantom_response ?? '')}
                  >
                    {String(row.phantom_response || '')}
                  </td>
                  <td className="clip">{String(row.message_variant || '')}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="pager">
          <button
            type="button"
            className="btn"
            disabled={page <= 1}
            onClick={() => setPage((p) => p - 1)}
          >
            Prev
          </button>
          <button
            type="button"
            className="btn"
            disabled={page >= totalPages}
            onClick={() => setPage((p) => p + 1)}
          >
            Next
          </button>
        </div>
      </div>
    </div>
  )
}
