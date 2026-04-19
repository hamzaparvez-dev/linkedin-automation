export type LeadRow = Record<string, string | number | null | undefined>

export interface Paginated<T> {
  page: number
  limit: number
  total: number
  items: T[]
  fieldnames?: string[]
  csv_path?: string
  error?: string
}

export interface StatsOverview {
  total_leads: number
  leads_by_status: { status: string; c: number }[]
  action_log_breakdown: {
    action_type: string
    dry_run: number
    status: string
    c: number
  }[]
  errors_last_24h_real: number
  metrics_daily_last_7d: {
    day: string
    account_id: string | null
    metric: string
    v: number
  }[]
}

export interface Health {
  ok: boolean
  database_path: string
  database_exists: boolean
  csv_path: string
  csv_exists: boolean
}
