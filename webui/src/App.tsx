import { useCallback, useEffect, useRef, useState } from "react"
import {
  Bell, BookOpen, CircleAlert, FileText, LayoutDashboard, Menu, RefreshCw, Settings as SettingsIcon, ShieldCheck, X,
} from "lucide-react"
import { Progress } from "./components/ui/progress"
import type { SimpleDoneFilter, SimpleDoneItem, SimpleDonePage, SimpleStatusResponse } from "./types/simple-status"
import { SimpleOverviewView } from "./views/SimpleOverviewView"
import { SimpleDoneView } from "./views/SimpleDoneView"
import { SimpleSettingsView } from "./views/SimpleSettingsView"
import { PendingView, type PendingRow } from "./views/PendingView"
import { MarkdownView, type MarkdownItem } from "./views/MarkdownView"

// Three workflows share one local console.
type View = "overview" | "pending" | "done" | "markdown" | "settings"

// ---- 共享类型（供高级维护与设置视图复用，不删除既有能力） ----

type PendingItem = {
  id: number; logical_item_id: number | null; occurrence_key: string; title: string | null
  sender: string | null; current_node: string | null; received_at: string | null; last_seen_at: string | null
  summary_status: string; feishu_status: string; cleanup_status: string
  occurrence_status: string; cleaned_at: string | null; notify_fingerprint: string | null; allow_renotify: boolean
}
type PendingDetail = {
  id: number; logical_item_id: number; title: string; sender: string | null; current_node: string | null
  identity: Record<string, boolean>; snapshot: null | { id: number; kind: string; version: number; payload: Record<string, unknown> }
  evidence_files: { id: number; role: string; status: string; local_relpath: string | null }[]
  attachments: { id: number; ordinal: number; role: string; name: string; status: string; error_code: string | null; retry_count: number; size_bytes: number | null; sha256: string | null; local_relpath: string | null; content_reused: boolean; knowledge_document_id: number | null; archive: null | { id: number; format: string; status: string; security_status: string; members: { id: number; path: string; status: string; error_code: string | null }[] } }[]
  lifecycle_pilot_status: string
  ollama_summary: null | { summary: string; matter_type: string; current_stage: string; key_points: string[]; required_action: string; confidence: number }
  ollama_summary_status: string
  cleanup_status: string; cleaned_at: string | null; notify_fingerprint: string | null; allow_renotify: boolean
  occurrence_status: string; feishu_status: string
  can_retry_delivery: boolean; requires_delivery_reconciliation: boolean; can_cleanup: boolean
  oa_gone_at: string | null
  discovery_hash: string | null
  stages: Record<"discovery" | "download" | "markdown" | "summary" | "feishu" | "cleanup", "pending" | "running" | "done" | "failed" | "review">
}
type ProcessingData = {
  historical_paused: boolean; historical_state: "idle" | "queued" | "running" | "paused"
  queues: { historical_done_backfill: Record<"queued" | "running" | "completed" | "failed", number> }
}
type OnlineAuditData = {
  run: null | {
    id: number; status: string; total_items: number; completed_items: number
    matched_items: number; mismatch_items: number; access_failed_items: number
    current_oa_item_key: string | null; missing_download_items: number
    local_extra_items: number; markdown_pending_items: number
  }
  items: { id: number; title: string; status: string; recognized_attachments: number | null; downloaded_attachments: number; comparison_reason: string | null; depth_limit_reached: boolean; error_code: string | null }[]
  comparison_reasons: Record<string, number>
}
type MarkdownDoc = {
  id: number; markdown_relpath: string; source_file: string | null; source_oa_item: string | null
  engine: string; quality: string; generated_at: string | null; oaradar_path: string
  llm_wiki_path: string; delivery_status: string
}
type SourceReview = {
  id: number; kind: string; item_id: number | null; file_id: number | null
  depth: number | null; details: { reason_code?: string; stage?: string }
  status: string; created_at: string | null
}
type SettingsData = {
  restart_required?: boolean
  pending_monitor: { feishu_enabled: boolean; llm_enabled: boolean }
  summary_model: Record<string, any>
  feishu: Record<string, any>
  data_cleanup: {
    auto_cleanup_after_success: boolean; cleanup_delay_hours: number; failed_retention_days: number
    keep_summary_body: boolean; keep_page_snapshot: boolean; keep_temp_attachments: boolean; allow_force_cleanup: boolean
  }
  done_archive: { enabled: boolean; archive_dir: string; compute_sha256: boolean; max_attachment_depth: number }
  markdown: Record<string, any>
  llm_wiki: { workspace_root: string; source_dir: string; source_dir_exists: boolean; source_dir_writable: boolean; write_frontmatter: boolean; atomic_publish: boolean }
}
type ServiceStatus = {
  installed: boolean; enabled: boolean; active: boolean
  last_started_at: string | null; last_error: string | null; next_run_at: string | null
}
type JobStatus = {
  job_id: number; found: boolean; status: string; stage: string
  progress_current: number; progress_total: number | null
  last_error_code: string | null; started_at: string | null; finished_at: string | null
  current_event: string | null
  run: null | { run_key: string; stage: string; status: string; started_at: string | null; finished_at: string | null; summary: Record<string, any> }
}
type ScheduleData = {
  recent_runs: { run_key: string; stage: string; status: string; started_at: string | null; finished_at: string | null; summary: Record<string, any> }[]
  last_scan_at: string | null
  overall_status: string
  services: Record<"web" | "worker" | "markdown_worker" | "hourly_timer" | "nightly_timer", ServiceStatus>
  system_info: { git_commit: string | null; build_time: string | null }
  schedule_available: boolean
  hourly_enabled: boolean | null
  next_run_at: string | null
  summary: {
    pending_new: number; pending_changed: number; done_new: number
    markdown_backlog: number
    feishu: { state: string; sent: number; failed: number }
    oa_login: { status: string; checked_at: string | null }
    nightly: {
      last_at: string | null; source_total: number; pages_scanned: number
      new_items: number; changed_items: number; baseline_hashes: number; retry_items: number
      knowledge_tasks_enqueued: number; download_jobs_enqueued: number
    }
  }
  notifications: { feishu_state: string; last_success_at: string | null; last_error_code: string | null; last_error_at: string | null; counts: Record<string, number> }
}
type MaintenanceData = {
  doctor: { ok: boolean; checks: { name: string; ok: boolean; required: boolean; detail?: string }[] }
  audit: { ok: boolean; issues: { code: string; detail: string }[] }
  capacity: Record<string, any>
}
type GovernanceRun = {
  id: number; status: string; rules_version: string; categories: string[]
  candidate_count: number; candidate_bytes: number
  quarantined_count: number; quarantined_bytes: number
  restored_count: number; restored_bytes: number; purged_count: number; purged_bytes: number
}
type GovernanceStorage = {
  disk_total_bytes: number; disk_free_bytes: number; database_bytes: number
  active_tasks: number; pending_reviews: number
  category_summary: Record<string, { count: number; bytes: number }>
  quarantine: { count: number; bytes: number; recoverable: boolean }
  originals: { items: number; files: number; bytes: number; protected: boolean }
  tiers: {
    id: string; label: string; retention: string; count: number; bytes: number
    database_references: number; protected: boolean
  }[]
}
type IntegrityAudit = {
  total: number; issue_counts: Record<string, number>; reason_counts: Record<string, number>
  finished_at: string | null
}
type ArchiveMigration = {
  status: string; progress_current: number; progress_total: number | null
  migrated: number; failed: number; review_required: number
}

const NAV = [
  { id: "overview" as View, label: "总览", icon: LayoutDashboard },
  { id: "pending" as View, label: "待办通知", icon: Bell },
  { id: "done" as View, label: "已办资料", icon: BookOpen },
  { id: "markdown" as View, label: "Markdown 输出", icon: FileText },
  { id: "settings" as View, label: "系统设置", icon: SettingsIcon },
]

async function api<T>(path: string): Promise<T> {
  const response = await fetch(path, { headers: { Accept: "application/json" } })
  const text = await response.text()
  if (!response.ok) throw new Error(`请求失败 (${response.status})`)
  try { return JSON.parse(text) as T } catch {
    if (text.trimStart().startsWith("<")) {
      throw new Error("后端返回了网页而非接口数据，可能是后端版本未更新，请重启 oaradar-web 服务后重试")
    }
    throw new Error("服务返回了非 JSON 内容，请刷新服务")
  }
}
const csrf = () => document.cookie.split("; ").find(row => row.startsWith("oa_csrf="))?.split("=")[1] || ""
async function postApi<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method: "POST",
    headers: { "x-csrf-token": csrf(), ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  })
  const text = await response.text()
  if (!response.ok) {
    let detail = text
    try { const parsed = JSON.parse(text); detail = typeof parsed.detail === "string" ? parsed.detail : `操作失败 (${response.status})` } catch { /* plain text fallback */ }
    throw new Error(detail || `操作失败 (${response.status})`)
  }
  try { return JSON.parse(text) as T } catch { return undefined as unknown as T }
}
async function patchApi<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, { method: "PATCH", headers: { "x-csrf-token": csrf(), "Content-Type": "application/json" }, body: JSON.stringify(body) })
  const data = await response.json()
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : `保存失败 (${response.status})`)
  return data as T
}
async function putApi<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, {
    method: "PUT",
    headers: { "x-csrf-token": csrf(), "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })
  const text = await response.text()
  if (!response.ok) {
    let detail = text
    try { const parsed = JSON.parse(text); detail = typeof parsed.detail === "string" ? parsed.detail : `操作失败 (${response.status})` } catch { /* plain text fallback */ }
    throw new Error(detail || `操作失败 (${response.status})`)
  }
  try { return JSON.parse(text) as T } catch { return undefined as unknown as T }
}
async function deleteApi<T>(path: string): Promise<T> {
  const response = await fetch(path, { method: "DELETE", headers: { "x-csrf-token": csrf() } })
  const text = await response.text()
  if (!response.ok) {
    let detail = text
    try { const parsed = JSON.parse(text); detail = typeof parsed.detail === "string" ? parsed.detail : `操作失败 (${response.status})` } catch { /* plain text fallback */ }
    throw new Error(detail || `操作失败 (${response.status})`)
  }
  try { return JSON.parse(text) as T } catch { return undefined as unknown as T }
}
const time = (value: string | null) => value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "-"
const size = (value: number | null) => value == null ? "-" : value < 1024 * 1024 ? `${(value / 1024).toFixed(1)} KB` : value < 1024 ** 3 ? `${(value / 1024 / 1024).toFixed(1)} MB` : `${(value / 1024 ** 3).toFixed(2)} GB`

function Badge({ tone = "neutral", children }: { tone?: "good" | "warn" | "bad" | "info" | "neutral"; children: React.ReactNode }) {
  return <span className={`status status-${tone}`}>{children}</span>
}
function Metric({ label, value, bad }: { label: string; value: number | string; bad?: boolean }) {
  return <div className={`metric ${bad ? "metric-bad" : ""}`}><span>{label}</span><strong>{typeof value === "number" ? value.toLocaleString() : value}</strong></div>
}
function Info({ label, value }: { label: string; value: string | number | null | undefined }) {
  return <div className="info"><span>{label}</span><strong>{value || "-"}</strong></div>
}
function SearchBox({ value, setValue, placeholder }: { value: string; setValue: (v: string) => void; placeholder: string }) {
  return <label className="search"><input value={value} onChange={e => setValue(e.target.value)} placeholder={placeholder}/></label>
}
function Field({ label, value, change }: { label: string; value: string; change: (v: string) => void }) {
  return <label className="setting-field"><span>{label}</span><input value={value} onChange={e => change(e.target.value)}/></label>
}
function NumberField({ label, value, change, step = "1" }: { label: string; value: number; change: (v: number) => void; step?: string }) {
  return <label className="setting-field"><span>{label}</span><input type="number" step={step} value={value} onChange={e => change(Number(e.target.value))}/></label>
}
function Toggle({ label, checked, change }: { label: string; checked: boolean; change: (v: boolean) => void }) {
  return <label className="setting-toggle"><span>{label}</span><input type="checkbox" checked={checked} onChange={e => change(e.target.checked)}/></label>
}
function SecretState({ label, configured }: { label: string; configured: boolean }) {
  return <div className="secret-state"><span>{label}</span><Badge tone={configured ? "good" : "warn"}>{configured ? "已配置" : "未配置"}</Badge></div>
}
function ServiceCard({ title, svc }: { title: string; svc: ServiceStatus }) {
  return <div className={`service-card ${svc.active ? "service-on" : svc.installed ? "service-idle" : "service-off"}`}>
    <header><strong>{title}</strong>{svc.active ? <Badge tone="good">运行中</Badge> : svc.installed ? <Badge tone="warn">未运行</Badge> : <Badge tone="neutral">未安装</Badge>}</header>
    <dl>
      <div><dt>启用</dt><dd>{svc.enabled ? "是" : "否"}</dd></div>
      <div><dt>最近启动</dt><dd>{time(svc.last_started_at)}</dd></div>
      <div><dt>最近错误</dt><dd className={svc.last_error ? "bad-text" : ""}>{svc.last_error || "无"}</dd></div>
    </dl>
  </div>
}

const SERVICE_TITLES: Record<string, string> = {
  web: "Web 服务", worker: "OA Worker", markdown_worker: "Markdown Worker",
  hourly_timer: "每小时定时器", nightly_timer: "夜间定时器",
}

// 供高级维护视图复用（不删除既有能力，仅折叠到高级维护）。
export {
  api, postApi, patchApi, putApi, deleteApi, csrf, time, size,
  Badge, Metric, Info, Progress, SearchBox, Field, NumberField, Toggle, SecretState, ServiceCard,
  SERVICE_TITLES,
}
export type {
  PendingItem, PendingDetail, ProcessingData, OnlineAuditData, MarkdownDoc, SourceReview,
  SettingsData, ServiceStatus, JobStatus, ScheduleData, MaintenanceData,
  GovernanceRun, GovernanceStorage, IntegrityAudit, ArchiveMigration,
}

type Route = { view: View; page: number; query: string; filter: string; selected: number | null }
function readRoute(): Route {
  const params = new URLSearchParams(window.location.hash.replace(/^#/, ""))
  const view = params.get("view") as View
  const positive = (v: string | null) => v && /^\d+$/.test(v) && Number(v) > 0 ? Number(v) : null
  return { view: NAV.some(n => n.id === view) ? view : "overview", page: positive(params.get("page")) || 1, query: params.get("query") || "", filter: params.get("filter") || "", selected: positive(params.get("item")) }
}
function routeHash(route: Route) {
  const params = new URLSearchParams({ view: route.view })
  if (route.page > 1) params.set("page", String(route.page))
  if (route.query) params.set("query", route.query)
  if (route.filter) params.set("filter", route.filter)
  if (route.selected) params.set("item", String(route.selected))
  return `#${params}`
}

export function App() {
  const [route, setRoute] = useState<Route>(readRoute)
  const { view, page, query, filter, selected } = route
  const routeRef = useRef(route); routeRef.current = route
  const dirty = useRef(false)
  const onDirtyChange = useCallback((value: boolean) => { dirty.current = value }, [])
  const [mobileNav, setMobileNav] = useState(false)
  const [loading, setLoading] = useState(false)
  const [loadedView, setLoadedView] = useState<View | null>(null)
  const [error, setError] = useState("")
  const [auth, setAuth] = useState<boolean | null>(null)
  const [token, setToken] = useState("")
  const [simpleStatus, setSimpleStatus] = useState<SimpleStatusResponse | null>(null)
  const [done, setDone] = useState<SimpleDoneItem[]>([])
  const [total, setTotal] = useState(0)
  const [doneMetrics, setDoneMetrics] = useState<SimpleDonePage["metrics"]>({ oa_done_total: 0, downloaded_items: 0, verified_attachments: 0 })
  const [settings, setSettings] = useState<SettingsData | null>(null)
  const [pending, setPending] = useState<PendingRow[]>([])
  const [markdown, setMarkdown] = useState<MarkdownItem[]>([])
  const requestSerial = useRef(0)
  const controller = useRef<AbortController | null>(null)

  const changeRoute = useCallback((patch: Partial<Route>, replace = false) => {
    const current = routeRef.current
    if (patch.view && patch.view !== current.view && dirty.current && !window.confirm("设置尚未保存，离开后将丢失修改。是否离开？")) return
    const next = { ...current, ...patch }
    if (patch.view && patch.view !== current.view) dirty.current = false
    window.history[replace ? "replaceState" : "pushState"](null, "", routeHash(next))
    routeRef.current = next; setRoute(next); setMobileNav(false); setError("")
  }, [])
  const navigate = useCallback((next: View, nextFilter = "", item: number | null = null) => {
    changeRoute({ view: next, query: "", filter: nextFilter, page: 1, selected: item })
  }, [changeRoute])
  useEffect(() => {
    const update = () => {
      const next = readRoute()
      if (dirty.current && next.view !== routeRef.current.view && !window.confirm("设置尚未保存，是否离开？")) {
        window.history.pushState(null, "", routeHash(routeRef.current)); return
      }
      if (next.view !== routeRef.current.view) dirty.current = false
      routeRef.current = next; setRoute(next); setError("")
    }
    window.addEventListener("popstate", update)
    window.addEventListener("hashchange", update)
    return () => { window.removeEventListener("popstate", update); window.removeEventListener("hashchange", update) }
  }, [])
  useEffect(() => {
    void api<{ required: boolean; authenticated: boolean }>("/api/auth/status").then(result => setAuth(!result.required || result.authenticated)).catch(reason => setError(String(reason)))
  }, [])
  const load = useCallback(async () => {
    if (!auth) return
    const serial = ++requestSerial.current
    controller.current?.abort()
    const abort = new AbortController(); controller.current = abort
    setLoading(true)
    try {
      const params = new URLSearchParams({ page: String(page), page_size: "50" })
      if (query) params.set("query", query)
      if (filter) {
        if (view === "markdown" && filter.startsWith("category:")) params.set("category", filter.slice(9))
        else params.set(view === "done" ? filter === "no_attachment" ? "attachment_review" : "simple_status" : view === "pending" ? "filter" : "status", filter)
      }
      const path = view === "overview" ? "/api/simple-status" : view === "settings" ? "/api/settings" : `/api/${view === "done" ? "done-archives" : view === "pending" ? "pending-notifications" : "markdown-outputs"}?${params}`
      const response = await fetch(path, { signal: abort.signal, headers: { Accept: "application/json" } })
      if (response.status === 401) { setAuth(false); throw new Error("本地会话已过期，请重新登录") }
      const result = await response.json()
      if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : `请求失败 (${response.status})`)
      if (serial !== requestSerial.current) return
      if (view === "overview") setSimpleStatus(result)
      else if (view === "settings") setSettings(result)
      else {
        if (!Array.isArray(result.items)) throw new Error(view === "markdown" ? "Markdown 页面需要 V2 Web API；请重启 oaradar-web 服务后重试" : "页面需要 V2 Web API；请重启 oaradar-web 服务后重试")
        const count = view === "markdown" ? result.item_total : result.total
        setTotal(count)
        if (page > Math.max(1, Math.ceil(count / 50))) { changeRoute({ page: Math.max(1, Math.ceil(count / 50)) }, true); return }
        if (view === "done") { setDone(result.items); setDoneMetrics(result.metrics) }
        else if (view === "pending") setPending(result.items)
        else setMarkdown(result.items)
      }
      setLoadedView(view)
      setError("")
    } catch (reason) {
      if (!abort.signal.aborted && serial === requestSerial.current) setError(reason instanceof Error ? reason.message : "读取失败")
    } finally { if (serial === requestSerial.current) setLoading(false) }
  }, [auth, view, page, query, filter, changeRoute])
  useEffect(() => { void load(); return () => controller.current?.abort() }, [load])
  useEffect(() => {
    if (!auth || view === "settings") return
    const timer = window.setInterval(() => { if (!document.hidden && !loading) void load() }, 5000)
    return () => window.clearInterval(timer)
  }, [auth, view, load, loading])
  const login = async (event: React.FormEvent) => {
    event.preventDefault(); setLoading(true); setError("")
    try { await postApi("/api/auth/login", { token }); setToken(""); setAuth(true) }
    catch (reason) { setError(reason instanceof Error ? reason.message : "登录失败") }
    finally { setLoading(false) }
  }
  const setPage = (value: number) => changeRoute({ page: value })
  const setQuery = (value: string) => changeRoute({ query: value, page: 1 }, true)
  const setFilter = (value: string) => changeRoute({ filter: value, page: 1, selected: null })
  const onSelect = (id: number | null) => changeRoute({ selected: id })
  if (auth === false) return <main className="login-page"><form onSubmit={event => void login(event)}><h1>OARadar 本地登录</h1><p>输入本机服务提供的访问令牌。</p><label>访问令牌<input type="password" value={token} onChange={event => setToken(event.target.value)} autoComplete="off" required/></label><button disabled={loading}>登录</button>{error && <p role="alert">{error}</p>}</form></main>
  return <div className="app-shell">
    <aside className={`sidebar ${mobileNav ? "sidebar-open" : ""}`}>
      <div className="brand"><span className="brand-mark">OA</span><div><strong>OARadar</strong><small>本地知识工作台</small></div></div>
      <nav aria-label="一级导航">{NAV.map(item => <button key={item.id} aria-current={view === item.id ? "page" : undefined} className={`nav-item ${view === item.id ? "nav-active" : ""}`} onClick={() => navigate(item.id)}><item.icon size={18}/><span>{item.label}</span></button>)}</nav>
      <div className="privacy"><ShieldCheck size={16}/><span>只读连接<br/><small>OA 内容仅保存在本机</small></span></div>
    </aside>
    <main className="workspace">
      <header className="topbar">
        <button className="icon-button menu-button" title="打开导航" aria-expanded={mobileNav} onClick={() => setMobileNav(!mobileNav)}><Menu size={19}/></button>
        <div><h1>{NAV.find(item => item.id === view)?.label}</h1><p>{view === "overview" ? "待办通知、原件归档与 Markdown 交付的独立进展" : view === "settings" ? "本地处理、通知与输出设置" : "查看事项进展、结果和需要处理的问题"}</p></div>
        <button className="icon-button refresh" title="刷新当前页面" onClick={() => void load()} disabled={loading}><RefreshCw size={18} className={loading ? "spin" : ""}/></button>
      </header>
      {error && <div className="error-banner" role="alert"><CircleAlert size={18}/><span>{error}</span><button title="关闭" onClick={() => setError("")}><X size={17}/></button></div>}
      {auth === null && <p>正在验证本地会话…</p>}
      {auth && loading && loadedView !== view && <p role="status" aria-live="polite">正在读取 OA 同步数据，请稍候…</p>}
      {auth && loadedView === view && <>
        {view === "overview" && simpleStatus && <SimpleOverviewView data={simpleStatus} onJump={navigate}/>}
        {view === "pending" && <PendingView rows={pending} total={total} page={page} pageSize={50} setPage={setPage} query={query} setQuery={setQuery} filter={filter} setFilter={setFilter} selectedId={selected} onSelect={onSelect} refresh={load}/>}
        {view === "done" && <SimpleDoneView rows={done} total={total} metrics={doneMetrics} page={page} setPage={setPage} query={query} setQuery={setQuery} filter={filter as SimpleDoneFilter | ""} setFilter={setFilter} selectedId={selected} onSelect={onSelect} refresh={load} onMarkdown={id => navigate("markdown", "", id)}/>}
        {view === "markdown" && <MarkdownView rows={markdown} total={total} page={page} setPage={setPage} query={query} setQuery={setQuery} filter={filter} setFilter={setFilter} selectedId={selected} onSelect={onSelect} refresh={load} onArchive={id => navigate("done", "", id)}/>}
        {view === "settings" && settings && <SimpleSettingsView initial={settings} onDirtyChange={onDirtyChange}/>}
      </>}
    </main>
  </div>
}
export default App
