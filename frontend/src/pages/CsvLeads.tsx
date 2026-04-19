import { useCallback, useEffect, useState } from 'react'
import { apiDownload, apiGet } from '../api'
import type { LeadRow, Paginated } from '../types'

export function CsvLeads() {
  const [page, setPage] = useState(1)
  const [q, setQ] = useState('')
  const [data, setData] = useState<Paginated<LeadRow> | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [dlErr, setDlErr] = useState<string | null>(null)
  const [downloading, setDownloading] = useState(false)
  const limit = 50

  const load = useCallback(async () => {
    setErr(null)
    try {
      const qs = new URLSearchParams({ page: String(page), limit: String(limit) })
      if (q.trim()) qs.set('q', q.trim())
      const res = await apiGet<Paginated<LeadRow>>(`/api/csv/leads?${qs}`)
      setData(res)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    }
  }, [page, q])

  useEffect(() => {
    void load()
  }, [load])

  const cols =
    data?.items?.[0] != null
      ? Object.keys(data.items[0] as object)
      : data?.fieldnames || []

  const totalPages = data ? Math.max(1, Math.ceil(data.total / limit)) : 1

  const downloadFullCsv = async () => {
    setDlErr(null)
    setDownloading(true)
    try {
      const base =
        data?.csv_path != null && data.csv_path !== ''
          ? data.csv_path.split(/[/\\]/).pop() || 'leads.csv'
          : 'leads.csv'
      await apiDownload('/api/csv/file', base)
    } catch (e) {
      setDlErr(e instanceof Error ? e.message : String(e))
    } finally {
      setDownloading(false)
    }
  }

  const canDownload = data != null && data.error !== 'file_not_found'

  return (
    <div className="stack">
      <div className="panel">
        <h2>CSV leads (merged / Web3)</h2>
        <p className="muted">
          Sourced from <code>output/leads_lookup_merged.csv</code> when present, else{' '}
          <code>output/apollo_leads.csv</code>. Override with <code>DASHBOARD_CSV_PATH</code> on the
          API process.
        </p>
        {data?.csv_path && (
          <p className="muted">
            Active file: <code>{data.csv_path}</code>
          </p>
        )}
        {data?.error === 'file_not_found' && (
          <p className="warn">CSV file not found — run pipeline or Web3 export first.</p>
        )}
        <div className="filters">
          <input
            className="input"
            placeholder="Filter rows (contains, case-insensitive)…"
            value={q}
            onChange={(e) => {
              setQ(e.target.value)
              setPage(1)
            }}
          />
          <button
            type="button"
            className="btn"
            disabled={!canDownload || downloading}
            onClick={() => void downloadFullCsv()}
            title="Download the full CSV file from disk (not limited to the current filter)"
          >
            {downloading ? 'Downloading…' : 'Download leads'}
          </button>
          <button type="button" className="btn" onClick={() => void load()}>
            Refresh
          </button>
        </div>
        {dlErr && <p className="error-text">{dlErr}</p>}
        {err && <p className="error-text">{err}</p>}
        {data && (
          <p className="muted">
            Page {data.page} of {totalPages} — {data.total} rows (filter in-memory)
          </p>
        )}
        <div className="table-wrap wide">
          <table>
            <thead>
              <tr>
                {cols.map((c) => (
                  <th key={c}>{c}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data?.items.map((row, i) => (
                <tr key={i}>
                  {cols.map((c) => (
                    <td key={c} className="clip">
                      {String((row as Record<string, unknown>)[c] ?? '')}
                    </td>
                  ))}
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
