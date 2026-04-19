const apiBase = (import.meta.env.VITE_API_BASE || '').replace(/\/$/, '')
const token = import.meta.env.VITE_DASHBOARD_TOKEN || ''

function headers(): HeadersInit {
  const h: Record<string, string> = { Accept: 'application/json' }
  if (token) h.Authorization = `Bearer ${token}`
  return h
}

export async function apiGet<T>(path: string): Promise<T> {
  const url = `${apiBase}${path.startsWith('/') ? path : `/${path}`}`
  const res = await fetch(url, { headers: headers() })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(`${res.status} ${res.statusText}: ${text.slice(0, 200)}`)
  }
  return res.json() as Promise<T>
}

function downloadHeaders(): HeadersInit {
  const h: Record<string, string> = { Accept: 'text/csv,*/*' }
  if (token) h.Authorization = `Bearer ${token}`
  return h
}

/** GET binary/text from API and trigger a browser download (supports Bearer auth). */
export async function apiDownload(path: string, fallbackFilename: string): Promise<void> {
  const url = `${apiBase}${path.startsWith('/') ? path : `/${path}`}`
  const res = await fetch(url, { headers: downloadHeaders() })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(`${res.status} ${res.statusText}: ${text.slice(0, 200)}`)
  }
  const blob = await res.blob()
  const cd = res.headers.get('content-disposition') || ''
  const m = /filename\*=UTF-8''([^;\s]+)|filename="([^"]+)"|filename=([^;\s]+)/i.exec(cd)
  const name = decodeURIComponent(m?.[1] || m?.[2] || m?.[3] || '').trim() || fallbackFilename
  const href = URL.createObjectURL(blob)
  try {
    const a = document.createElement('a')
    a.href = href
    a.download = name
    a.rel = 'noopener'
    a.click()
  } finally {
    URL.revokeObjectURL(href)
  }
}
