import { useEffect, useState } from 'react'
import { apiGet } from '../api'

interface AccountOut {
  account_id: string
  profile_name: string
  user_agent: string | null
  schedule_start: string
  schedule_end: string
  primary_strategy: string
  delay_min_sec: number
  delay_max_sec: number
  steady_connect_cap: number
  steady_dm_cap: number
  steady_reply_cap: number
  weekend_actions: boolean
  phantombuster_connect_agent_id: string | null
  phantombuster_dm_agent_id: string | null
  daily_usage_today: { connects: number; dms: number; replies: number }
  accounts_meta_row: Record<string, unknown> | null
}

interface AccountsResponse {
  campaign_id: string | null
  accounts: AccountOut[]
  config_error?: string
}

export function AccountsPage() {
  const [data, setData] = useState<AccountsResponse | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const res = await apiGet<AccountsResponse>('/api/accounts')
        if (!cancelled) setData(res)
      } catch (e) {
        if (!cancelled) setErr(e instanceof Error ? e.message : String(e))
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  if (err) return <div className="panel error">{err}</div>
  if (!data) return <div className="panel">Loading…</div>

  return (
    <div className="stack">
      <div className="panel">
        <h2>Accounts</h2>
        {data.config_error && (
          <p className="warn">
            Config: {data.config_error} — showing <code>accounts_meta</code> only if present.
          </p>
        )}
        <p className="muted">
          Campaign: <strong>{data.campaign_id || '—'}</strong>
        </p>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Account</th>
                <th>Strategy</th>
                <th>Schedule</th>
                <th>Caps (connect / dm / reply)</th>
                <th>Usage today</th>
                <th>User-Agent</th>
                <th>PB connect agent</th>
                <th>PB DM agent</th>
              </tr>
            </thead>
            <tbody>
              {data.accounts.map((a) => (
                <tr key={a.account_id}>
                  <td>
                    <strong>{a.account_id}</strong>
                    <div className="muted small">{a.profile_name}</div>
                  </td>
                  <td>{a.primary_strategy}</td>
                  <td>
                    {a.schedule_start}–{a.schedule_end}
                  </td>
                  <td>
                    {a.steady_connect_cap} / {a.steady_dm_cap} / {a.steady_reply_cap}
                  </td>
                  <td>
                    {a.daily_usage_today.connects} / {a.daily_usage_today.dms} /{' '}
                    {a.daily_usage_today.replies}
                  </td>
                  <td className="mono small" title={a.user_agent || ''}>
                    {a.user_agent
                      ? a.user_agent.length > 48
                        ? `${a.user_agent.slice(0, 48)}…`
                        : a.user_agent
                      : '—'}
                  </td>
                  <td className="mono small">{a.phantombuster_connect_agent_id || '—'}</td>
                  <td className="mono small">{a.phantombuster_dm_agent_id || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
