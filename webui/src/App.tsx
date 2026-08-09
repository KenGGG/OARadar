import { useCallback, useEffect, useState } from "react"
import {
  Archive, Bell, BookOpen, ChevronRight, CircleAlert, Database, FileCheck2,
  FileText, Gauge, HardDrive, LayoutDashboard, Loader2, Menu, RefreshCw, Search,
  Server, Settings as SettingsIcon, ShieldCheck, Trash2, X, RotateCcw, Save,
  BrainCircuit, ListChecks,
} from "lucide-react"
import { Progress } from "./components/ui/progress"

// 业务链路导航（plan-0807-1 §3）。设置单独置于底部。
type View = "overview" | "pending" | "done" | "markdown" | "settings"

type DashboardData = {
  generated_at: string
  pending_notification: {
    status: string; last_scan_at: string | null; next_scan_at: string | null
    feishu_success: number; feishu_failed: number; awaiting_cleanup: number; cleanup_failed: number
  }
  done_archive: {
    status: string; oa_done_total: number; downloaded_items: number
    verified_attachments: number; download_failed: number
  }
  markdown_delivery: {
    status: string; markdown_total: number; exported: number; pending: number; failed: number
    source_dir: string; source_dir_exists: boolean; source_dir_writable: boolean
  }
  needs_attention: { code: string; label: string; severity: "error" | "warning"; jump: string }[]
}

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
  oa_gone_at: string | null
  discovery_hash: string | null
}
type DoneItem = {
  id: number; item_id: string; title: string; sender: string | null; completed_at: string | null
  pipeline_status: string; archive_relpath: string | null; file_count: number | null
  archive_status_label: string | null; markdown: { label: string; status: string }
  handoff: { label: string; status: string }; local_dir: string | null
}
type DoneMetrics = { oa_done_total: number; downloaded_items: number; verified_attachments: number }
type MarkdownDoc = {
  id: number; markdown_relpath: string; source_file: string | null; source_oa_item: string | null
  engine: string; quality: string; generated_at: string | null; oaradar_path: string
  llm_wiki_path: string; delivery_status: string
}
type SettingsData = {
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
    nightly: { last_at: string | null; markdown_tasks_enqueued: number; download_jobs_enqueued: number }
  }
  notifications: { feishu_state: string; last_success_at: string | null; last_error_code: string | null; last_error_at: string | null; counts: Record<string, number> }
}
type MaintenanceData = {
  doctor: { ok: boolean; checks: { name: string; ok: boolean; required: boolean; detail?: string }[] }
  audit: { ok: boolean; issues: { code: string; detail: string }[] }
  capacity: Record<string, any>
}

const nav = [
  { id: "overview" as View, label: "总览", icon: LayoutDashboard },
  { id: "pending" as View, label: "待办通知", icon: FileCheck2 },
  { id: "done" as View, label: "已办归档", icon: Archive },
  { id: "markdown" as View, label: "Markdown 输出", icon: BookOpen },
]
const bottomNav = [
  { id: "settings" as View, label: "设置", icon: SettingsIcon },
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
  if (!response.ok) throw new Error(text || `操作失败 (${response.status})`)
  try { return JSON.parse(text) as T } catch { return undefined as unknown as T }
}
const time = (value: string | null) => value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "-"
const size = (value: number | null) => value == null ? "-" : value < 1024 * 1024 ? `${(value / 1024).toFixed(1)} KB` : `${(value / 1024 / 1024).toFixed(1)} MB`

// 状态中文化（plan-0807-1 §11）
const CLEANUP_LABELS: Record<string, string> = {
  not_eligible: "不适用", pending_cleanup: "等待清理", cleaning: "清理中", cleaned: "已清理", cleanup_failed: "清理失败",
}
const FEISHU_LABELS: Record<string, string> = {
  sent: "已发送", failed: "发送失败", rejected: "发送失败", misconfigured: "配置异常",
  pending: "待发送", queued: "待发送", retry_wait: "等待重试", unknown: "结果待确认",
}
const SUMMARY_LABELS: Record<string, string> = { pending: "待生成", current: "已生成", failed: "生成失败" }
const cleanupLabel = (s: string) => CLEANUP_LABELS[s] || s
const cleanupTone = (s: string): "good" | "warn" | "bad" | "neutral" =>
  s === "cleaned" ? "good" : s === "cleanup_failed" ? "bad" : s === "pending_cleanup" || s === "cleaning" ? "warn" : "neutral"
const feishuLabel = (s: string) => FEISHU_LABELS[s] || s
const feishuTone = (s: string): "good" | "warn" | "bad" | "neutral" =>
  s === "sent" ? "good" : s === "failed" || s === "rejected" || s === "misconfigured" ? "bad" : "warn"
const summaryLabel = (s: string) => SUMMARY_LABELS[s] || s
const summaryTone = (s: string): "good" | "warn" | "bad" | "neutral" =>
  s === "current" ? "good" : s === "failed" ? "bad" : "neutral"
const archiveTone = (value: string): "good" | "warn" | "bad" | "neutral" =>
  value === "download_failed" ? "bad" : ["downloaded", "archived", "files_verified", "parsed"].includes(value) ? "good" : ["scanning", "partial"].includes(value) ? "warn" : "neutral"
const captureLabel = (value: string | null) => value === "pending_initial" ? "首次采集" : value === "pending_updated" ? "更新采集" : value || "已采集"

function Badge({ tone = "neutral", children }: { tone?: "good" | "warn" | "bad" | "info" | "neutral"; children: React.ReactNode }) {
  return <span className={`status status-${tone}`}>{children}</span>
}

export function App() {
  const [view, setView] = useState<View>("overview")
  const [mobileNav, setMobileNav] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const [query, setQuery] = useState("")
  const [dashboard, setDashboard] = useState<DashboardData | null>(null)
  const [pending, setPending] = useState<PendingItem[]>([])
  const [pendingTotal, setPendingTotal] = useState(0)
  const [pendingFilter, setPendingFilter] = useState<string>("")
  const [done, setDone] = useState<DoneItem[]>([])
  const [doneTotal, setDoneTotal] = useState(0)
  const [doneMetrics, setDoneMetrics] = useState<DoneMetrics>({ oa_done_total: 0, downloaded_items: 0, verified_attachments: 0 })
  const [donePage, setDonePage] = useState(1)
  const [doneArchiveFilter, setDoneArchiveFilter] = useState("")
  const [doneMdFilter, setDoneMdFilter] = useState("")
  const [doneHandoffFilter, setDoneHandoffFilter] = useState("")
  const [markdown, setMarkdown] = useState<MarkdownDoc[]>([])
  const [settings, setSettings] = useState<SettingsData | null>(null)
  const [pendingDetail, setPendingDetail] = useState<PendingDetail | null>(null)
  const [syncing, setSyncing] = useState(false)

  const navigate = useCallback((next: View) => {
    setView(next); setQuery(""); setPendingFilter(""); setPendingDetail(null); setMobileNav(false)
  }, [])

  const load = useCallback(async (silent = false) => {
    if (!silent) { setLoading(true); setError("") }
    try {
      if (view === "overview") setDashboard(await api<DashboardData>("/api/dashboard"))
      else if (view === "pending") {
        const result = await api<{ items: PendingItem[]; total: number }>(`/api/pending-notifications${pendingFilter ? `?filter=${pendingFilter}` : ""}`)
        setPending(result.items); setPendingTotal(result.total)
      }
      else if (view === "done") {
        const params = new URLSearchParams({ page: String(donePage), page_size: "100" })
        if (query) params.set("query", query)
        if (doneArchiveFilter) params.set("archive_status", doneArchiveFilter)
        if (doneMdFilter) params.set("markdown_status", doneMdFilter)
        if (doneHandoffFilter) params.set("handoff_status", doneHandoffFilter)
        const result = await api<{ items: DoneItem[]; total: number; metrics: DoneMetrics }>(`/api/done-archives?${params.toString()}`)
        setDone(result.items); setDoneTotal(result.total); setDoneMetrics(result.metrics)
      }
      else if (view === "markdown") setMarkdown((await api<{ documents: MarkdownDoc[] }>("/api/markdown-outputs")).documents)
      else if (view === "settings") setSettings(await api<SettingsData>("/api/settings"))
    } catch (reason) { setError(reason instanceof Error ? reason.message : "操作失败") }
    finally { if (!silent) setLoading(false) }
  }, [view, pendingFilter, donePage, query, doneArchiveFilter, doneMdFilter, doneHandoffFilter])

  useEffect(() => { void load() }, [load])
  useEffect(() => { if (view !== "pending") return; const timer = window.setInterval(() => void load(true), 5000); return () => window.clearInterval(timer) }, [view, load])

  const openPending = async (id: number) => {
    setError("")
    try { setPendingDetail(await api<PendingDetail>(`/api/pending-notifications/${id}`)) }
    catch (reason) { setError(reason instanceof Error ? reason.message : "详情加载失败") }
  }

  // 待办通知页刷新 = 触发与 OA 实时同步（重发现待办列表），而非仅重载本地库。
  const resyncPending = useCallback(async () => {
    setError(""); setSyncing(true)
    try {
      await postApi("/api/schedule/hourly", {})
      // 作业交给常驻 worker 执行；列表由 5s 自动轮询刷新，这里先拉一次本地状态。
      await load()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "与 OA 同步失败")
    } finally { setSyncing(false) }
  }, [load])

  const topLabel = (id: View) => [...nav, ...bottomNav].find(item => item.id === id)?.label || ""

  return <div className="app-shell">
    <aside className={`sidebar ${mobileNav ? "sidebar-open" : ""}`}>
      <div className="brand"><span className="brand-mark">OA</span><div><strong>OARadar</strong><small>本地知识工作台</small></div></div>
      <nav aria-label="一级导航">{nav.map(item => <button key={item.id} className={`nav-item ${view === item.id ? "nav-active" : ""}`} onClick={() => navigate(item.id)}><item.icon size={18}/><span>{item.label}</span></button>)}</nav>
      <div className="sidebar-bottom">
        {bottomNav.map(item => <button key={item.id} className={`nav-item ${view === item.id ? "nav-active" : ""}`} onClick={() => navigate(item.id)}><item.icon size={18}/><span>{item.label}</span></button>)}
      </div>
      <div className="privacy"><ShieldCheck size={16}/><span>只读连接<br/><small>OA 内容仅保存在本机</small></span></div>
    </aside>
    <main className="workspace">
      <header className="topbar">
        <button className="icon-button menu-button" title="打开导航" onClick={() => setMobileNav(!mobileNav)}><Menu size={19}/></button>
        <div><h1>{topLabel(view)}</h1><p>{view === "overview" ? "三条自动化链路是否正常，当前有哪些事项需要人工处理" : view === "pending" ? "短期待办通知处理台，已发送并清理的不再长期展示" : view === "done" ? "已办事项的原始附件是否完整归档并生成 Markdown" : view === "markdown" ? "哪些 OA 原始文件已成功转换为 Markdown 并交付" : "按业务链路划分的系统设置与运行维护"}</p></div>
        <button className="icon-button refresh" title={view === "pending" ? "与 OA 同步最新待办列表" : "刷新当前页面"} onClick={() => view === "pending" ? void resyncPending() : void load()} disabled={loading || syncing}><RefreshCw size={18} className={loading || syncing ? "spin" : ""}/></button>
        {syncing && <span className="sync-hint">正在与 OA 同步…</span>}
      </header>
      {error && <div className="error-banner"><CircleAlert size={18}/><span>{error}</span><button title="关闭" onClick={() => setError("")}><X size={17}/></button></div>}
      {loading ? <div className="loading"><Loader2 className="spin"/><span>正在读取本地状态</span></div> : <>
        {view === "overview" && dashboard && <OverviewView data={dashboard} onJump={navigate}/>}
        {view === "pending" && <PendingNotificationsView rows={pending} total={pendingTotal} filter={pendingFilter} setFilter={setPendingFilter} query={query} setQuery={setQuery} open={openPending} reload={load}/>}
        {view === "done" && <DoneArchivesView rows={done} total={doneTotal} metrics={doneMetrics} page={donePage} setPage={setDonePage} query={query} setQuery={setQuery} archiveFilter={doneArchiveFilter} setArchiveFilter={setDoneArchiveFilter} mdFilter={doneMdFilter} setMdFilter={setDoneMdFilter} handoffFilter={doneHandoffFilter} setHandoffFilter={setDoneHandoffFilter} reload={load}/>}
        {view === "markdown" && <MarkdownOutputsView rows={markdown} reload={load}/>}
        {view === "settings" && settings && <SettingsView data={settings} reload={load}/>}
      </>}
    </main>
    {pendingDetail && <PendingDrawer data={pendingDetail} close={() => setPendingDetail(null)}/>}
  </div>
}

// ---------------------------------------------------------------------------
// 总览（plan §4）
// ---------------------------------------------------------------------------

function ChainCard({ title, status, icon: Icon, children }: { title: string; status: "normal" | "abnormal"; icon: React.ComponentType<{ size?: number }>; children: React.ReactNode }) {
  return <div className={`chain-card ${status === "abnormal" ? "chain-abnormal" : "chain-ok"}`}>
    <header><Icon size={18}/><strong>{title}</strong><Badge tone={status === "normal" ? "good" : "bad"}>{status === "normal" ? "正常" : "异常"}</Badge></header>
    <div className="chain-body">{children}</div>
  </div>
}

function OverviewView({ data, onJump }: { data: DashboardData; onJump: (view: View) => void }) {
  const pn = data.pending_notification
  const da = data.done_archive
  const md = data.markdown_delivery
  const jump = (target: string) => {
    if (target === "maintenance") onJump("settings")
    else if (target === "pending" || target === "done" || target === "settings") onJump(target)
  }
  return <section>
    <p className="overview-updated">数据更新于 {time(data.generated_at)}</p>
    <div className="chain-grid">
      <ChainCard title="待办通知链路" status={pn.status === "normal" ? "normal" : "abnormal"} icon={Bell}>
        {pn.status === "normal"
          ? <p className="chain-simple">待办通知运行正常<br/><small>最近扫描：{time(pn.last_scan_at)} · 下次扫描：{time(pn.next_scan_at)}</small></p>
          : <>
            {pn.feishu_failed > 0 && <div className="chain-alert" onClick={() => jump("pending")}><CircleAlert size={16}/>{pn.feishu_failed} 条飞书发送失败</div>}
            {pn.cleanup_failed > 0 && <div className="chain-alert" onClick={() => jump("pending")}><CircleAlert size={16}/>{pn.cleanup_failed} 条待办数据清理失败</div>}
            <div className="chain-meta"><span>飞书成功 {pn.feishu_success}</span><span>待清理 {pn.awaiting_cleanup}</span></div>
          </>}
      </ChainCard>
      <ChainCard title="已办归档链路" status={da.status === "normal" ? "normal" : "abnormal"} icon={Archive}>
        {da.status === "normal"
          ? <p className="chain-simple">已办归档运行正常<br/><small>已归档 {da.downloaded_items} / 共 {da.oa_done_total} 项</small></p>
          : <div className="chain-alert" onClick={() => jump("done")}><CircleAlert size={16}/>{da.download_failed} 项附件下载失败</div>}
        <div className="chain-meta"><span>已验证附件 {da.verified_attachments}</span></div>
      </ChainCard>
      <ChainCard title="Markdown 交付链路" status={md.status === "normal" ? "normal" : "abnormal"} icon={BrainCircuit}>
        {md.status === "normal"
          ? <p className="chain-simple">Markdown 交付正常<br/><small>已交付 {md.exported} / 共 {md.markdown_total} 份</small></p>
          : <div className="chain-alert" onClick={() => jump("markdown")}><CircleAlert size={16}/>{md.failed} 份 Markdown 转换失败</div>}
        <div className="chain-meta">
          <span>待交付 {md.pending}</span>
          <span className={md.source_dir_exists ? "" : "bad-text"}>来源目录{md.source_dir_exists ? "存在" : "不存在"}</span>
          <span className={md.source_dir_writable ? "" : "bad-text"}>{md.source_dir_writable ? "可写" : "不可写"}</span>
        </div>
        <p className="chain-path" title={md.source_dir}>{md.source_dir}</p>
      </ChainCard>
    </div>
    <div className="section-toolbar"><div><h2>需要人工处理</h2><p>仅显示真正需要干预的问题。</p></div></div>
    {data.needs_attention.length
      ? <div className="attention-list">{data.needs_attention.map(item => <button key={item.code} className={`attention-item ${item.severity}`} onClick={() => jump(item.jump)}><CircleAlert size={16}/><span className="label">{item.label}</span><ChevronRight size={16}/></button>)}</div>
      : <div className="empty panel">当前没有需要人工处理的问题。</div>}
  </section>
}

// ---------------------------------------------------------------------------
// 待办通知（plan §5）
// ---------------------------------------------------------------------------

const PENDING_FILTERS = [
  { key: "", label: "全部" },
  { key: "processing", label: "处理中" },
  { key: "summary_failed", label: "摘要失败" },
  { key: "feishu_failed", label: "飞书失败" },
  { key: "awaiting_cleanup", label: "等待清理" },
  { key: "cleanup_failed", label: "清理失败" },
  { key: "recent_success", label: "最近成功" },
]

function PendingNotificationsView({ rows, total, filter, setFilter, query, setQuery, open, reload }: {
  rows: PendingItem[]; total: number; filter: string; setFilter: (v: string) => void
  query: string; setQuery: (v: string) => void; open: (id: number) => void; reload: () => Promise<void>
}) {
  const runAction = async (label: string, path: string, body?: unknown) => {
    try { await postApi(path, body); await reload() }
    catch (reason) { alert(`${label}失败：${reason instanceof Error ? reason.message : "未知错误"}`) }
  }
  const cleanupAll = () => { if (confirm("清理所有符合条件（飞书已发送）的历史待办数据？")) void runAction("一键清理", "/api/pending-notifications/cleanup-eligible") }
  return <section>
    <div className="metrics compact-metrics"><Metric label="当前记录" value={total}/><Metric label="等待清理" value={rows.filter(r => r.cleanup_status === "pending_cleanup" || r.cleanup_status === "not_eligible").length}/><Metric label="清理失败" value={rows.filter(r => r.cleanup_status === "cleanup_failed").length}/><Metric label="飞书失败" value={rows.filter(r => r.feishu_status === "failed").length}/></div>
    <div className="section-toolbar"><div><h2>待办通知</h2><p>短期通知处理台；已发送并完成清理的不再长期展示业务内容。</p></div><div className="toolbar-actions"><button onClick={cleanupAll}><Trash2 size={16}/>一键清理符合条件</button></div></div>
    <div className="filter-row">{PENDING_FILTERS.map(f => <button key={f.key} className={`chip ${filter === f.key ? "chip-active" : ""}`} onClick={() => setFilter(f.key)}>{f.label}</button>)}{<SearchBox value={query} setValue={setQuery} placeholder="搜索标题或发起人"/>}</div>
    <div className="table-wrap"><table style={{ minWidth: 1080 }}><thead><tr><th className="title-col">事项</th><th>发起人</th><th>当前节点</th><th>发现时间</th><th>摘要状态</th><th>飞书状态</th><th>清理状态</th><th aria-label="操作"/></tr></thead><tbody>
      {rows.map(row => {
        const title = row.cleanup_status === "cleaned" || row.occurrence_status === "cleaned"
          ? `脱敏编号 #${row.logical_item_id ?? row.id}`
          : (row.title || "（无标题）")
        return <tr key={row.id} onClick={() => open(row.id)} tabIndex={0} onKeyDown={e => e.key === "Enter" && open(row.id)}>
          <td className="title-cell"><strong>{title}</strong><small>{row.occurrence_key}</small></td>
          <td>{row.cleanup_status === "cleaned" ? "—" : (row.sender || "-")}</td>
          <td>{row.cleanup_status === "cleaned" ? "—" : (row.current_node || "-")}</td>
          <td className="nowrap">{time(row.received_at)}</td>
          <td><Badge tone={summaryTone(row.summary_status)}>{summaryLabel(row.summary_status)}</Badge></td>
          <td><Badge tone={feishuTone(row.feishu_status)}>{feishuLabel(row.feishu_status)}</Badge></td>
          <td><Badge tone={cleanupTone(row.cleanup_status)}>{cleanupLabel(row.cleanup_status)}</Badge></td>
          <td><ChevronRight size={17}/></td>
        </tr>
      })}
      {!rows.length && <tr><td colSpan={8} className="empty">没有符合条件的待办通知</td></tr>}
    </tbody></table></div>
  </section>
}

function PendingDrawer({ data, close }: { data: PendingDetail; close: () => void }) {
  const [acting, setActing] = useState("")
  const [detail, setDetail] = useState<PendingDetail>(data)
  const [syncing, setSyncing] = useState(false)
  const cleaned = detail.cleanup_status === "cleaned" || detail.occurrence_status === "cleaned"
  const oaGone = detail.oa_gone_at != null
  const run = async (label: string, path: string, body?: unknown) => {
    setActing(label)
    try { await postApi(path, body); alert(`${label}已触发，请稍后在列表中查看结果`); }
    catch (reason) { alert(`${label}失败：${reason instanceof Error ? reason.message : "未知错误"}`) }
    finally { setActing("") }
  }
  // 打开已清理事项时，自动与 OA 同步以恢复标题/发起人等展示信息（仅展示，不落库业务正文）。
  useEffect(() => {
    if (!cleaned || oaGone || detail.title) return
    let cancelled = false
    let tries = 0
    const poll = async () => {
      setSyncing(true)
      try {
        await postApi(`/api/pending-notifications/${detail.id}/sync-oa`)
        while (!cancelled && tries < 8) {
          tries += 1
          await new Promise(r => setTimeout(r, 2000))
          const updated = await api<PendingDetail>(`/api/pending-notifications/${detail.id}`)
          if (updated.title || updated.oa_gone_at) { setDetail(updated); break }
        }
      } catch (reason) {
        console.error("与 OA 同步失败", reason)
      } finally {
        if (!cancelled) setSyncing(false)
      }
    }
    void poll()
    return () => { cancelled = true }
  }, [cleaned, oaGone, detail.id, detail.title])
  const ollama = detail.ollama_summary
  const titleText = syncing ? "正在与 OA 同步…"
    : oaGone ? `脱敏编号 #${detail.logical_item_id}（已在 OA 中处理/移除）`
    : cleaned ? `脱敏编号 #${detail.logical_item_id}` : (detail.title || "（无标题）")
  return <div className="drawer-layer" role="dialog" aria-modal="true"><button className="drawer-scrim" aria-label="关闭详情" onClick={close}/><aside className="drawer">
    <header><div><small>待办通知详情</small><h2>{titleText}</h2></div><button className="icon-button" title="关闭" onClick={close}><X size={19}/></button></header>
    <div className="drawer-body">
      <div className="detail-grid"><Info label="发起人" value={cleaned || syncing ? null : detail.sender}/><Info label="当前节点" value={cleaned || syncing ? null : detail.current_node}/><Info label="摘要状态" value={summaryLabel(detail.ollama_summary_status)}/><Info label="飞书状态" value={feishuLabel(detail.feishu_status)}/><Info label="清理状态" value={cleanupLabel(detail.cleanup_status)}/><Info label="清理时间" value={detail.cleaned_at}/></div>
      {ollama && <><h3>通知内容</h3><div className="detail-grid"><Info label="事项类型" value={ollama.matter_type}/><Info label="当前阶段" value={ollama.current_stage}/><Info label="需采取行动" value={ollama.required_action}/><Info label="置信度" value={String(ollama.confidence)}/></div><pre className="markdown-preview">{ollama.summary}</pre></>}
      <h3>处理状态</h3>
      <div className="pipeline-steps"><Step label="发现" done/><Step label="内容采集" done={!!detail.snapshot}/><Step label="摘要" done={detail.ollama_summary_status === "current"}/><Step label="飞书" done={detail.feishu_status === "sent"} warn={detail.feishu_status === "failed"}/><Step label="清理" done={detail.cleanup_status === "cleaned"} warn={detail.cleanup_status === "cleanup_failed"}/></div>
      <div className="drawer-actions">
        <button disabled={!!acting || syncing} onClick={() => { setDetail(data); void (async () => {
          setSyncing(true)
          try { await postApi(`/api/pending-notifications/${detail.id}/sync-oa`); setDetail(await api<PendingDetail>(`/api/pending-notifications/${detail.id}`)) }
          catch (reason) { alert(`与 OA 同步失败：${reason instanceof Error ? reason.message : "未知错误"}`) }
          finally { setSyncing(false) }
        })() }}><RefreshCw size={16}/>与 OA 同步</button>
        <button disabled={!!acting} onClick={() => void run("重试摘要", `/api/pending-notifications/${detail.id}/retry-summary`)}><RotateCcw size={16}/>重试摘要</button>
        <button disabled={!!acting} onClick={() => void run("重试发送", `/api/pending-notifications/${detail.id}/retry-delivery`)}><Bell size={16}/>重试发送</button>
        <button className="button-danger" disabled={!!acting} onClick={() => { if (confirm("立即清理该待办的业务数据？将仅保留最小去重台账。")) void run("清理", `/api/pending-notifications/${detail.id}/cleanup`, { force: true }) }}><Trash2 size={16}/>立即清理</button>
      </div>
      <details className="advanced"><summary>高级诊断</summary>
        <div className="detail-grid">
          <Info label="逻辑事项 ID" value={String(detail.logical_item_id)}/>
          <Info label="Discovery Hash" value={detail.discovery_hash}/>
          <Info label="通知指纹" value={detail.notify_fingerprint}/>
          <Info label="允许重通知" value={detail.allow_renotify ? "是" : "否"}/>
          <Info label="OA 已移除" value={oaGone ? "是" : "否"}/>
          <Info label="身份采集" value={Object.entries(detail.identity).map(([k, v]) => `${k}:${v ? "✓" : "✗"}`).join(" ")}/>
          <Info label="附件数" value={String(detail.attachments.length)}/>
        </div>
      </details>
    </div>
  </aside></div>
}

function Step({ label, done, warn }: { label: string; done?: boolean; warn?: boolean }) {
  return <span className={`step ${done ? "step-done" : warn ? "step-warn" : "step-pending"}`}>{label}</span>
}

// ---------------------------------------------------------------------------
// 已办归档（plan §7）
// ---------------------------------------------------------------------------

function DoneArchivesView({ rows, total, metrics, page, setPage, query, setQuery, archiveFilter, setArchiveFilter, mdFilter, setMdFilter, handoffFilter, setHandoffFilter, reload }: {
  rows: DoneItem[]; total: number; metrics: DoneMetrics; page: number; setPage: (p: number) => void
  query: string; setQuery: (v: string) => void
  archiveFilter: string; setArchiveFilter: (v: string) => void
  mdFilter: string; setMdFilter: (v: string) => void
  handoffFilter: string; setHandoffFilter: (v: string) => void
  reload: () => Promise<void>
}) {
  const pages = Math.max(1, Math.ceil(total / 100))
  const runAction = async (label: string, path: string) => {
    try { await postApi(path); await reload() }
    catch (reason) { alert(`${label}失败：${reason instanceof Error ? reason.message : "未知错误"}`) }
  }
  return <section>
    <div className="metrics"><Metric label="OA已办事项" value={metrics.oa_done_total}/><Metric label="成功下载事项" value={metrics.downloaded_items}/><Metric label="已验证附件" value={metrics.verified_attachments}/></div>
    <div className="section-toolbar"><div><h2>已办归档</h2><p>搜索与筛选由后端执行，覆盖全库。</p></div><div className="toolbar-actions"><SearchBox value={query} setValue={setQuery} placeholder="搜索标题/发起人/事项ID"/></div></div>
    <div className="filter-row">
      <select value={archiveFilter} onChange={e => setArchiveFilter(e.target.value)}><option value="">原始归档：全部</option><option value="完整归档">完整归档</option><option value="确认无附件">确认无附件</option><option value="部分缺失">部分缺失</option><option value="下载失败">下载失败</option><option value="正在下载">正在下载</option></select>
      <select value={mdFilter} onChange={e => setMdFilter(e.target.value)}><option value="">Markdown：全部</option><option value="转换成功">转换成功</option><option value="待转换">待转换</option><option value="转换中">转换中</option><option value="转换失败">转换失败</option></select>
      <select value={handoffFilter} onChange={e => setHandoffFilter(e.target.value)}><option value="">llm_wiki 交付：全部</option><option value="已交付">已交付</option><option value="待交付">待交付</option><option value="来源目录不可用">来源目录不可用</option><option value="交付失败">交付失败</option></select>
    </div>
    <div className="table-wrap"><table style={{ minWidth: 1180 }}><thead><tr><th className="title-col">事项标题</th><th>发起人</th><th>办结时间</th><th>已验证附件</th><th>原始归档</th><th>Markdown</th><th>llm_wiki 交付</th><th>本地目录</th><th aria-label="操作"/></tr></thead><tbody>
      {rows.map(row => <tr key={row.id}>
        <td className="title-cell"><strong>{row.title}</strong><small>OA事项ID {row.item_id}</small></td>
        <td>{row.sender || "-"}</td>
        <td className="nowrap">{time(row.completed_at)}</td>
        <td>{row.file_count == null ? "-" : row.file_count}</td>
        <td><Badge tone={archiveTone(row.pipeline_status)}>{row.archive_status_label || row.pipeline_status}</Badge></td>
        <td><Badge tone={row.markdown.status === "exported" ? "good" : row.markdown.status === "export_failed" ? "bad" : "warn"}>{row.markdown.label}</Badge></td>
        <td><Badge tone={row.handoff.status === "exported" ? "good" : row.handoff.status === "unavailable" || row.handoff.status === "export_failed" ? "bad" : "warn"}>{row.handoff.label}</Badge></td>
        <td className="path-cell" title={row.local_dir || ""}>{row.local_dir || "-"}</td>
        <td><button className="row-action" title="重试失败步骤" onClick={() => void runAction("重试", `/api/done-archives/${row.id}/retry-archive`)}><RotateCcw size={15}/></button></td>
      </tr>)}
      {!rows.length && <tr><td colSpan={9} className="empty">没有符合条件的已办归档</td></tr>}
    </tbody></table></div>
    <div className="pagination"><button disabled={page <= 1} onClick={() => setPage(page - 1)}>上一页</button><span>第 {page}/{pages} 页</span><button disabled={page >= pages} onClick={() => setPage(page + 1)}>下一页</button></div>
  </section>
}

// ---------------------------------------------------------------------------
// Markdown 输出（plan §8）
// ---------------------------------------------------------------------------

function MarkdownOutputsView({ rows, reload }: { rows: MarkdownDoc[]; reload: () => Promise<void> }) {
  const runAction = async (label: string, path: string) => {
    try { await postApi(path); await reload() }
    catch (reason) { alert(`${label}失败：${reason instanceof Error ? reason.message : "未知错误"}`) }
  }
  return <section>
    <div className="metrics"><Metric label="Markdown 总数" value={rows.length}/><Metric label="已交付" value={rows.filter(r => r.delivery_status === "exported").length}/><Metric label="待交付" value={rows.filter(r => r.delivery_status !== "exported").length}/></div>
    <div className="section-toolbar"><div><h2>Markdown 输出</h2><p>已成功转换为 Markdown 并交付给 llm_wiki 来源目录的原始文件。</p></div></div>
    <div className="table-wrap"><table style={{ minWidth: 1100 }}><thead><tr><th className="title-col">Markdown 文件</th><th>原始附件</th><th>来源事项</th><th>解析引擎</th><th>质量</th><th>输出时间</th><th>OARadar 路径</th><th>交付状态</th><th aria-label="操作"/></tr></thead><tbody>
      {rows.map(row => <tr key={row.id}>
        <td className="title-cell"><strong>{row.markdown_relpath}</strong><small>llm_wiki: {row.llm_wiki_path}</small></td>
        <td>{row.source_file || "-"}</td>
        <td>{row.source_oa_item || "-"}</td>
        <td>{row.engine}</td>
        <td><Badge tone={row.quality === "passed" ? "good" : row.quality === "failed" ? "bad" : "warn"}>{row.quality}</Badge></td>
        <td className="nowrap">{time(row.generated_at)}</td>
        <td className="path-cell" title={row.oaradar_path}>{row.oaradar_path}</td>
        <td><Badge tone={row.delivery_status === "exported" ? "good" : "bad"}>{row.delivery_status === "exported" ? "已交付" : "待交付"}</Badge></td>
        <td><button className="row-action" title="重新生成" onClick={() => void runAction("重新生成", `/api/markdown-outputs/${row.id}/rebuild`)}><RotateCcw size={15}/></button></td>
      </tr>)}
      {!rows.length && <tr><td colSpan={9} className="empty">尚无 Markdown 输出</td></tr>}
    </tbody></table></div>
  </section>
}

// ---------------------------------------------------------------------------
// 设置（plan §9）
// ---------------------------------------------------------------------------

function SettingsView({ data, reload }: { data: SettingsData; reload: () => Promise<void> }) {
  const [form, setForm] = useState<SettingsData>(data)
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState("")
  const [schedule, setSchedule] = useState<ScheduleData | null>(null)
  const [diag, setDiag] = useState<MaintenanceData | null>(null)
  const [acting, setActing] = useState("")

  useEffect(() => { setForm(data) }, [data])
  useEffect(() => { void (async () => { try { setSchedule(await api<ScheduleData>("/api/schedule/status")) } catch { /* ignore */ } })() }, [])
  useEffect(() => { void (async () => { try { setDiag(await api<MaintenanceData>("/api/maintenance")) } catch { /* ignore */ } })() }, [])

  const save = async () => {
    setSaving(true); setMessage("")
    const payload = {
      llm: {
        enabled: form.summary_model.enabled, active_provider: form.summary_model.active_provider,
        ollama_base_url: form.summary_model.ollama_base_url, ollama_model: form.summary_model.ollama_model,
        agnes_base_url: form.summary_model.agnes_base_url, agnes_model: form.summary_model.agnes_model,
        timeout_seconds: Number(form.summary_model.timeout_seconds), max_tokens: Number(form.summary_model.max_tokens),
        temperature: Number(form.summary_model.temperature), max_retries: Number(form.summary_model.max_retries),
        max_concurrency: Number(form.summary_model.max_concurrency),
      },
      feishu: {
        enabled: form.feishu.enabled, message_type: form.feishu.message_type,
        max_items_per_section: Number(form.feishu.max_items_per_section), redact_confidential: form.feishu.redact_confidential,
        retry_attempts: Number(form.feishu.retry_attempts),
      },
      pending_cleanup: { ...form.data_cleanup },
      markdown_export: { ...form.markdown },
    }
    try {
      await postApi("/api/settings", payload)
      setMessage("设置已保存，需要重启服务后生效")
      await reload()
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : "保存失败") }
    finally { setSaving(false) }
  }

  const action = async (label: string, path: string, body?: unknown) => {
    setActing(label)
    try { await postApi(path, body); setMessage(`${label}已触发`); setSchedule(await api<ScheduleData>("/api/schedule/status")); }
    catch (reason) { setMessage(`${label}失败：${reason instanceof Error ? reason.message : "未知错误"}`) }
    finally { setActing("") }
  }

  const toggle = (group: "summary_model" | "feishu" | "data_cleanup" | "markdown", key: string, value: any) =>
    setForm(f => ({ ...f, [group]: { ...(f[group] as any), [key]: value } }))

  return <section className="settings-stack">
    {/* 9.1 待办监控与飞书 */}
    <div className="settings-panel"><h2><Bell size={18}/>待办监控与飞书</h2>
      <div className="settings-sub"><h3>扫描计划</h3>
        <Toggle label="启用待办监控（飞书）" checked={form.feishu.enabled} change={v => toggle("feishu", "enabled", v)}/>
        <Toggle label="启用智能摘要" checked={form.summary_model.enabled} change={v => toggle("summary_model", "enabled", v)}/>
      </div>
      <div className="settings-sub"><h3>摘要模型</h3>
        <div className="provider-choice">
          <button className={form.summary_model.active_provider === "ollama" ? "provider-active" : ""} onClick={() => toggle("summary_model", "active_provider", "ollama")}><strong>本地 Ollama</strong><small>首选</small></button>
          <button className={form.summary_model.active_provider === "agnes" ? "provider-active" : ""} onClick={() => toggle("summary_model", "active_provider", "agnes")}><strong>Agnes</strong><small>备用</small></button>
        </div>
        <div className="field-pair">
          <Field label="API 地址" value={form.summary_model.active_provider === "ollama" ? form.summary_model.ollama_base_url : form.summary_model.agnes_base_url} change={v => toggle("summary_model", form.summary_model.active_provider === "ollama" ? "ollama_base_url" : "agnes_base_url", v)}/>
          <Field label="模型" value={form.summary_model.active_provider === "ollama" ? form.summary_model.ollama_model : form.summary_model.agnes_model} change={v => toggle("summary_model", form.summary_model.active_provider === "ollama" ? "ollama_model" : "agnes_model", v)}/>
        </div>
        <div className="field-pair">
          <NumberField label="超时（秒）" value={form.summary_model.timeout_seconds} change={v => toggle("summary_model", "timeout_seconds", v)}/>
          <NumberField label="最大输出" value={form.summary_model.max_tokens} change={v => toggle("summary_model", "max_tokens", v)}/>
          <NumberField label="温度" value={form.summary_model.temperature} step="0.1" change={v => toggle("summary_model", "temperature", v)}/>
          <NumberField label="最大并发" value={form.summary_model.max_concurrency} change={v => toggle("summary_model", "max_concurrency", v)}/>
        </div>
        <SecretState label={form.feishu.webhook_env || "FEISHU_WEBHOOK"} configured={!!form.feishu.webhook_configured}/>
        <SecretState label={form.feishu.secret_env || "FEISHU_SECRET"} configured={!!form.feishu.secret_configured}/>
      </div>
      <div className="settings-sub"><h3>飞书通知</h3>
        <div className="field-pair">
          <NumberField label="单次最大事项数" value={form.feishu.max_items_per_section} change={v => toggle("feishu", "max_items_per_section", v)}/>
          <NumberField label="重试次数" value={form.feishu.retry_attempts} change={v => toggle("feishu", "retry_attempts", v)}/>
        </div>
        <Toggle label="通知内容脱敏" checked={form.feishu.redact_confidential} change={v => toggle("feishu", "redact_confidential", v)}/>
        <button onClick={() => void action("测试飞书", "/api/notifications/test")}><Bell size={16}/>测试飞书</button>
      </div>
      <div className="settings-sub"><h3>数据清理</h3>
        <Toggle label="飞书成功后自动清理" checked={form.data_cleanup.auto_cleanup_after_success} change={v => toggle("data_cleanup", "auto_cleanup_after_success", v)}/>
        <div className="field-pair">
          <NumberField label="清理延迟（小时）" value={form.data_cleanup.cleanup_delay_hours} change={v => toggle("data_cleanup", "cleanup_delay_hours", v)}/>
          <NumberField label="失败数据保留天数" value={form.data_cleanup.failed_retention_days} change={v => toggle("data_cleanup", "failed_retention_days", v)}/>
        </div>
        <Toggle label="保留摘要正文" checked={form.data_cleanup.keep_summary_body} change={v => toggle("data_cleanup", "keep_summary_body", v)}/>
        <Toggle label="保留页面快照" checked={form.data_cleanup.keep_page_snapshot} change={v => toggle("data_cleanup", "keep_page_snapshot", v)}/>
        <Toggle label="保留临时附件" checked={form.data_cleanup.keep_temp_attachments} change={v => toggle("data_cleanup", "keep_temp_attachments", v)}/>
        <Toggle label="允许人工强制清理" checked={form.data_cleanup.allow_force_cleanup} change={v => toggle("data_cleanup", "allow_force_cleanup", v)}/>
      </div>
    </div>

    {/* 9.2 已办归档（只读摘要） */}
    <div className="settings-panel"><h2><Archive size={18}/>已办归档</h2>
      <div className="detail-grid">
        <Info label="已办监控" value={data.done_archive.enabled ? "已启用" : "未启用"}/>
        <Info label="永久归档根目录" value={data.done_archive.archive_dir}/>
        <Info label="计算 SHA256" value={data.done_archive.compute_sha256 ? "是" : "否"}/>
        <Info label="压缩包展开深度" value={String(data.done_archive.max_attachment_depth)}/>
      </div>
    </div>

    {/* 9.3 Markdown 转换 */}
    <div className="settings-panel"><h2><BookOpen size={18}/>Markdown 转换</h2>
      <Toggle label="启用 Markdown 转换" checked={form.markdown.enabled} change={v => toggle("markdown", "enabled", v)}/>
      <Field label="中间输出目录" value={String(form.markdown.source_markdown_dir ?? "")} change={v => toggle("markdown", "source_markdown_dir", v)}/>
      <Field label="Workspace 根目录" value={String(form.markdown.workspace_root ?? "")} change={v => toggle("markdown", "workspace_root", v)}/>
      <Toggle label="保持来源目录结构" checked={!!form.markdown.preserve_source_tree} change={v => toggle("markdown", "preserve_source_tree", v)}/>
      <Toggle label="写入 YAML frontmatter" checked={!!form.markdown.write_frontmatter} change={v => toggle("markdown", "write_frontmatter", v)}/>
      <Toggle label="原子发布" checked={!!form.markdown.atomic_publish} change={v => toggle("markdown", "atomic_publish", v)}/>
    </div>

    {/* 9.4 llm_wiki 交接（只读摘要） */}
    <div className="settings-panel"><h2><BrainCircuit size={18}/>llm_wiki 交接</h2>
      <div className="detail-grid">
        <Info label="Workspace 根目录" value={data.llm_wiki.workspace_root}/>
        <Info label="来源目录" value={data.llm_wiki.source_dir}/>
        <Info label="来源目录存在" value={data.llm_wiki.source_dir_exists ? "是" : "否"}/>
        <Info label="来源目录可写" value={data.llm_wiki.source_dir_writable ? "是" : "否"}/>
        <Info label="写入 frontmatter" value={data.llm_wiki.write_frontmatter ? "是" : "否"}/>
        <Info label="原子发布" value={data.llm_wiki.atomic_publish ? "是" : "否"}/>
      </div>
    </div>

    {/* 9.5 运行维护 */}
    <div className="settings-panel"><h2><Server size={18}/>运行维护</h2>
      <p className="settings-note">仅用于异常处理。危险操作会改动本机服务状态。</p>
      {schedule && <div className="service-grid-5">{Object.entries(schedule.services).map(([key, svc]) => <ServiceCard key={key} title={SERVICE_TITLES[key] || key} svc={svc}/>)}</div>}
      <div className="toolbar-actions">
        <button disabled={!!acting} onClick={() => void action("立即扫描待办", "/api/schedule/hourly")}><RefreshCw size={16} className={acting === "立即扫描待办" ? "spin" : ""}/>立即扫描待办</button>
        <button disabled={!!acting} onClick={() => void action("立即扫描已办", "/api/schedule/nightly")}><Database size={16}/>立即夜间补齐</button>
        <button disabled={!!acting} onClick={() => void action("重试失败任务", "/api/maintenance/actions", { action: "retry_failed" })}><RotateCcw size={16}/>重试失败任务</button>
        <button disabled={!!acting} onClick={() => void action("启用自动运行", "/api/schedule/control", { action: "enable" })}>启用自动运行</button>
        <button className="button-danger" disabled={!!acting} onClick={() => void action("暂停自动运行", "/api/schedule/control", { action: "disable" })}>暂停自动运行</button>
        <button className="button-danger" disabled={!!acting} onClick={() => void action("重启 Worker", "/api/schedule/control", { action: "restart_worker" })}>重启 Worker</button>
        <button className="button-danger" disabled={!!acting} onClick={() => void action("重新登录 OA", "/api/schedule/control", { action: "relogin" })}>重新登录 OA</button>
      </div>
      {diag && <>
        <div className="section-toolbar"><div><h3>诊断检查</h3></div></div>
        <div className="data-grid">
          <div className={`data-cell ${diag.doctor.ok ? "" : "data-cell-bad"}`}><span>健康检查</span><strong>{diag.doctor.ok ? "通过" : "存在问题"}</strong></div>
          <div className={`data-cell ${diag.audit.ok ? "" : "data-cell-bad"}`}><span>数据库审计</span><strong>{diag.audit.ok ? "正常" : `${diag.audit.issues.length} 项`}</strong></div>
        </div>
        {!diag.doctor.ok && <ul className="diag-list">{diag.doctor.checks.filter(c => !c.ok).map(c => <li key={c.name} className={c.required ? "bad-text" : ""}>{c.name}{c.detail ? `：${c.detail}` : ""}</li>)}</ul>}
      </>}
    </div>

    <div className="settings-head"><button className="button-primary" onClick={() => void save()} disabled={saving}><Save size={16}/>{saving ? "保存中" : "保存设置"}</button></div>
    {message && <div className="settings-message">{message}</div>}
  </section>
}

const SERVICE_TITLES: Record<string, string> = {
  web: "Web 服务", worker: "OA Worker", markdown_worker: "Markdown Worker",
  hourly_timer: "每小时定时器", nightly_timer: "夜间定时器",
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

// ---------------------------------------------------------------------------
// 共用小组件
// ---------------------------------------------------------------------------

function SearchBox({ value, setValue, placeholder }: { value: string; setValue: (v: string) => void; placeholder: string }) {
  return <label className="search"><Search size={17}/><input value={value} onChange={e => setValue(e.target.value)} placeholder={placeholder}/></label>
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
function Metric({ label, value, bad }: { label: string; value: number | string; bad?: boolean }) {
  return <div className={`metric ${bad ? "metric-bad" : ""}`}><span>{label}</span><strong>{typeof value === "number" ? value.toLocaleString() : value}</strong></div>
}
function Info({ label, value }: { label: string; value: string | number | null | undefined }) {
  return <div className="info"><span>{label}</span><strong>{value || "-"}</strong></div>
}

export default App
