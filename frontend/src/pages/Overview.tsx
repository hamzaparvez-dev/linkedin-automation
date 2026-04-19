import { useEffect, useMemo, useState } from 'react'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { apiGet } from '../api'
import type { Health, StatsOverview } from '../types'

export function Overview() {
  const [health, setHealth] = useState<Health | null>(null)
  const [stats, setStats] = useState<StatsOverview | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const [h, s] = await Promise.all([
          apiGet<Health>('/api/health'),
          apiGet<StatsOverview>('/api/stats/overview'),
        ])
        if (!cancelled) {
          setHealth(h)
          setStats(s)
        }
      } catch (e) {
        if (!cancelled) setErr(e instanceof Error ? e.message : String(e))
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  const chartData = useMemo(() => {
    if (!stats?.metrics_daily_last_7d?.length) return []
    const byDay = new Map<string, number>()
    for (const r of stats.metrics_daily_last_7d) {
      const k = r.day
      byDay.set(k, (byDay.get(k) || 0) + Number(r.v || 0))
    }
    return [...byDay.entries()]
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([day, total]) => ({ day, total }))
  }, [stats])

  const connects = useMemo(() => {
    if (!stats?.action_log_breakdown) return { real: 0, dry: 0 }
    let real = 0
    let dry = 0
    for (const r of stats.action_log_breakdown) {
      if (r.action_type !== 'connect') continue
      if (r.status !== 'ok') continue
      const n = Number(r.c)
      if (r.dry_run) dry += n
      else real += n
    }
    return { real, dry }
  }, [stats])

  const dms = useMemo(() => {
    if (!stats?.action_log_breakdown) return { real: 0, dry: 0 }
    let real = 0
    let dry = 0
    for (const r of stats.action_log_breakdown) {
      if (r.action_type !== 'dm') continue
      if (r.status !== 'ok') continue
      const n = Number(r.c)
      if (r.dry_run) dry += n
      else real += n
    }
    return { real, dry }
  }, [stats])

  if (err) {
    return (
      <div className="panel error">
        <h2>Could not load dashboard</h2>
        <p>{err}</p>
        <p className="muted">
          Start the API from the repo root:{' '}
          <code>python3 -m uvicorn dashboard_app.main:app --reload --port 8080</code>
        </p>
      </div>
    )
  }

  return (
    <div className="stack">
      <div className="panel">
        <h2>System</h2>
        {health ? (
          <ul className="kv">
            <li>
              <span>Database</span>
              <code>{health.database_path}</code>
            </li>
            <li>
              <span>DB file exists</span>
              <strong>{health.database_exists ? 'yes' : 'no'}</strong>
            </li>
            <li>
              <span>CSV (Web3 / merged)</span>
              <code>{health.csv_path}</code>
            </li>
            <li>
              <span>CSV exists</span>
              <strong>{health.csv_exists ? 'yes' : 'no'}</strong>
            </li>
          </ul>
        ) : (
          <p>Loading…</p>
        )}
      </div>

      {stats && (
        <>
          <div className="kpi-grid">
            <div className="kpi">
              <div className="kpi-label">Total leads (SQLite)</div>
              <div className="kpi-value">{stats.total_leads}</div>
            </div>
            <div className="kpi">
              <div className="kpi-label">Connects sent (real / dry-run)</div>
              <div className="kpi-value">
                {connects.real} / {connects.dry}
              </div>
            </div>
            <div className="kpi">
              <div className="kpi-label">DMs sent (real / dry-run)</div>
              <div className="kpi-value">
                {dms.real} / {dms.dry}
              </div>
            </div>
            <div className="kpi">
              <div className="kpi-label">PB errors (24h, non–dry-run)</div>
              <div className="kpi-value warn">{stats.errors_last_24h_real}</div>
            </div>
          </div>

          <div className="panel">
            <h2>Leads by status</h2>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Status</th>
                    <th>Count</th>
                  </tr>
                </thead>
                <tbody>
                  {stats.leads_by_status.map((r) => (
                    <tr key={r.status}>
                      <td>
                        <span className="badge">{r.status}</span>
                      </td>
                      <td>{r.c}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          <div className="panel">
            <h2>Metrics (last 7 days, summed)</h2>
            {chartData.length === 0 ? (
              <p className="muted">No metrics_daily rows yet — run engagement after connects succeed.</p>
            ) : (
              <div className="chart-box">
                <ResponsiveContainer width="100%" height={280}>
                  <BarChart data={chartData}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#333" />
                    <XAxis dataKey="day" tick={{ fill: '#9ca3af', fontSize: 11 }} />
                    <YAxis tick={{ fill: '#9ca3af', fontSize: 11 }} />
                    <Tooltip
                      contentStyle={{ background: '#111', border: '1px solid #333' }}
                    />
                    <Legend />
                    <Bar dataKey="total" name="All metrics" fill="#6366f1" />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  )
}
