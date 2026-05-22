import { useCallback, useEffect, useRef, useState } from 'react'
import { apiGet, apiPostFormData } from '../api'
import type { LeadRow, Paginated } from '../types'

type ImportResult = { imported: number; skipped: number; errors: string[] }

export function Leads() {
  const [page, setPage] = useState(1)
  const [q, setQ] = useState('')
  const [status, setStatus] = useState('')
  const [data, setData] = useState<Paginated<LeadRow> | null>(null)
  const [detail, setDetail] = useState<LeadRow | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [csvFile, setCsvFile] = useState<File | null>(null)
  const [importing, setImporting] = useState(false)
  const [importOk, setImportOk] = useState<string | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const limit = 40

  const load = useCallback(async () => {
    setErr(null)
    try {
      const qs = new URLSearchParams({
        page: String(page),
        limit: String(limit),
      })
      if (q.trim()) qs.set('q', q.trim())
      if (status.trim()) qs.set('status', status.trim())
      const res = await apiGet<Paginated<LeadRow>>(`/api/leads?${qs}`)
      setData(res)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    }
  }, [page, q, status])

  useEffect(() => {
    void load()
  }, [load])

  const openDetail = async (leadId: string) => {
    try {
      const row = await apiGet<LeadRow>(`/api/leads/${encodeURIComponent(leadId)}`)
      setDetail(row)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    }
  }

  const totalPages = data ? Math.max(1, Math.ceil(data.total / limit)) : 1

  const onCsvChosen = (f: File | null) => {
    setImportOk(null)
    if (!f) {
      setCsvFile(null)
      return
    }
    if (!f.name.toLowerCase().endsWith('.csv')) {
      setErr('Please choose a .csv file.')
      setCsvFile(null)
      return
    }
    setErr(null)
    setCsvFile(f)
  }

  const doCsvUpload = async () => {
    if (!csvFile) return
    setImporting(true)
    setImportOk(null)
    setErr(null)
    try {
      const fd = new FormData()
      fd.append('file', csvFile)
      const res = await apiPostFormData<ImportResult>('/api/leads/import', fd)
      let msg = `Imported ${res.imported} lead(s). Skipped: ${res.skipped}.`
      if (res.errors?.length) {
        msg += ` (${res.errors.slice(0, 3).join(' · ')})`
      }
      setImportOk(msg)
      setCsvFile(null)
      if (fileInputRef.current) fileInputRef.current.value = ''
      await load()
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setImporting(false)
    }
  }

  return (
    <div className="stack">
      <div className="panel">
        <h2>Leads (SQLite)</h2>
        <div className="csv-import-zone">
          <p className="muted" style={{ marginTop: 0 }}>
            <strong>Post-text CSV import</strong> — export your sheet as UTF-8 CSV with columns:{' '}
            <code>Name</code>, <code>Headline</code>, <code>Profile Url</code>, <code>Post text</code>,{' '}
            <code>Post url</code>, <code>occupation</code>. Personal <code>/in/</code> profiles only (company
            URLs are skipped). After upload, run <code>python main.py --promote-post-leads</code> on the server.
          </p>
          <div className="row">
            <input
              ref={fileInputRef}
              type="file"
              accept=".csv,text/csv"
              style={{ display: 'none' }}
              onChange={(e) => onCsvChosen(e.target.files?.[0] ?? null)}
            />
            <button type="button" className="btn" onClick={() => fileInputRef.current?.click()}>
              Choose CSV
            </button>
            <span className="muted small">{csvFile ? csvFile.name : 'No file selected'}</span>
            <button
              type="button"
              className="btn"
              disabled={!csvFile || importing}
              onClick={() => void doCsvUpload()}
            >
              {importing ? 'Uploading…' : 'Upload'}
            </button>
          </div>
          {importOk && <p className="success-text">{importOk}</p>}
        </div>
        <p className="muted">
          <strong>Empty LinkedIn / email?</strong> Apollo <code>api_search</code> alone is incomplete.
          Use <code>APOLLO_EXTRACT_BULK_MATCH=true</code> (default) when scraping, run the main pipeline so
          NEW leads with a LinkedIn URL are promoted to ENRICHED and scored, and ensure leads reach{' '}
          <code>QUALIFIED</code> for <code>account_id</code> assignment.
        </p>
        <div className="filters">
          <input
            className="input"
            placeholder="Search name, company, title, URL…"
            value={q}
            onChange={(e) => {
              setQ(e.target.value)
              setPage(1)
            }}
          />
          <input
            className="input narrow"
            placeholder="Status filter"
            value={status}
            onChange={(e) => {
              setStatus(e.target.value)
              setPage(1)
            }}
          />
          <button type="button" className="btn" onClick={() => void load()}>
            Refresh
          </button>
        </div>
        {err && <p className="error-text">{err}</p>}
        {data && (
          <p className="muted">
            Page {data.page} of {totalPages} — {data.total} total
          </p>
        )}
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Email</th>
                <th>Title</th>
                <th>Company</th>
                <th>Status</th>
                <th>Score</th>
                <th>Account</th>
                <th>Apollo ID</th>
                <th>Campaign</th>
                <th>LinkedIn</th>
              </tr>
            </thead>
            <tbody>
              {data?.items.map((row) => (
                <tr
                  key={String(row.lead_id)}
                  className="click"
                  onClick={() => void openDetail(String(row.lead_id))}
                >
                  <td>{String(row.full_name || '')}</td>
                  <td className="clip">{String(row.email || '')}</td>
                  <td>{String(row.title || '')}</td>
                  <td>{String(row.company_name || '')}</td>
                  <td>
                    <span className="badge">{String(row.status || '')}</span>
                  </td>
                  <td>{String(row.score ?? '')}</td>
                  <td>{String(row.account_id || '')}</td>
                  <td className="mono small">{String(row.apollo_person_id || '')}</td>
                  <td className="clip">{String(row.campaign_id || '')}</td>
                  <td>
                    {row.linkedin_url ? (
                      <a
                        href={String(row.linkedin_url)}
                        target="_blank"
                        rel="noreferrer"
                        onClick={(e) => e.stopPropagation()}
                      >
                        Open
                      </a>
                    ) : (
                      '—'
                    )}
                  </td>
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

      {detail && (
        <div className="panel detail">
          <div className="detail-head">
            <h3>{String(detail.full_name || detail.lead_id)}</h3>
            <button type="button" className="btn ghost" onClick={() => setDetail(null)}>
              Close
            </button>
          </div>
          {(detail.linkedin_headline || detail.title || detail.post_url || detail.post_text) && (
            <div className="post-lead-fields">
              {detail.linkedin_headline ? (
                <p>
                  <strong>Headline:</strong> {String(detail.linkedin_headline)}
                </p>
              ) : null}
              {detail.title ? (
                <p>
                  <strong>Role:</strong> {String(detail.title)}
                </p>
              ) : null}
              {detail.post_url ? (
                <p>
                  <strong>Post:</strong>{' '}
                  <a href={String(detail.post_url)} target="_blank" rel="noreferrer">
                    Open
                  </a>
                </p>
              ) : null}
              {detail.post_text ? (
                <p className="post-text-snippet">
                  <strong>Post text:</strong>{' '}
                  {String(detail.post_text).length > 400
                    ? `${String(detail.post_text).slice(0, 400)}…`
                    : String(detail.post_text)}
                </p>
              ) : null}
            </div>
          )}
          <pre className="json-block">{JSON.stringify(detail, null, 2)}</pre>
        </div>
      )}
    </div>
  )
}
