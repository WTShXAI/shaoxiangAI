// 轻量同源 fetch 封装 (不修改 services/api.ts 单一真相源)。
// 用于 api.ts 尚未封装的端点 (cross-book / risk/status / report/equity / portfolio)。
export async function apiFetch<T = any>(path: string, opts?: RequestInit): Promise<T> {
  const BRIDGE = ((import.meta as any).env?.VITE_BRIDGE_URL || '').trim()
  const url = `${BRIDGE}${path}`
  const resp = await window.fetch(url, {
    headers: { 'Content-Type': 'application/json', ...(opts?.headers || {}) },
    ...opts,
  })
  const text = await resp.text()
  if (!resp.ok) {
    throw new Error(`HTTP ${resp.status}: ${text.slice(0, 200)}`)
  }
  try {
    return JSON.parse(text) as T
  } catch {
    return text as unknown as T
  }
}
