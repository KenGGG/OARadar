import { useCallback, useEffect, useState } from "react"
import {
  Archive, BookOpen, ChevronRight, CircleAlert, Database, FileCheck2,
  FileText, Gauge, HardDrive, Loader2, Menu, RefreshCw, Search, Server,
  ShieldCheck, X, Save, Bell, BrainCircuit, ListChecks,
} from "lucide-react"
import { Progress } from "./components/ui/progress"

type View = "pending" | "done" | "processing" | "knowledge" | "audit" | "system" | "autorun"
type PendingItem = {
  id: number; logical_item_id: number; title: string; sender: string | null
  received_at: string | null; current_node: string | null; processing_status: string
  identity_captured: boolean; snapshot_kind: string | null; snapshot_id: number | null
  body_status: string; workflow_status: string; opinion_status: string
  attachment_total: number; attachment_verified: number; attachment_failed: number
  last_synced_at: string | null; last_discovered_at: string | null; last_summary_at: string | null
  feishu_status: string | null; last_notified_at: string | null; notify_error_code: string | null
  ollama_summary_status: string
}
type PendingDetail = {
  id: number; logical_item_id: number; title: string; sender: string | null; current_node: string | null
  identity: Record<string, boolean>; snapshot: null | { id: number; kind: string; version: number; payload: Record<string, unknown> }
  evidence_files: { id: number; role: string; status: string; local_relpath: string | null }[]
  attachments: { id: number; ordinal: number; role: string; name: string; status: string; error_code: string | null; retry_count: number; size_bytes: number | null; sha256: string | null; local_relpath: string | null; content_reused: boolean; knowledge_document_id: number | null; archive: null | { id: number; format: string; status: string; security_status: string; members: { id: number; path: string; status: string; error_code: string | null }[] } }[]
  lifecycle_pilot_status: string
  ollama_summary: null | { summary: string; matter_type: string; current_stage: string; key_points: string[]; required_action: string; confidence: number }
  ollama_summary_status: string
}
type DoneItem = { id: number; item_id: string; title: string; sender: string | null; completed_at: string | null; pipeline_status: string; archive_relpath: string | null; file_count: number | null }
type DoneMetrics = { oa_done_total: number; downloaded_items: number; verified_attachments: number }
type KnowledgeItem = { id: number; title: string; publish_status: string; vault_relpath: string; active_parse_artifact_id: number | null; source_count: number }
type KnowledgeDetail = KnowledgeItem & { preview: string; artifact: null | { id: number; engine: string; quality_score: number; status: string; output_relpath: string }; sources: { oa_item_id: number | null; source_file_id: number | null }[] }
type SystemData = { web: { status: string; url: string }; worker: null | { id: number; type: string; status: string; progress_current: number; progress_total: number | null; current_title: string | null; attachment_verified: number; attachment_total: number; failure_count: number }; mineru: Record<string, unknown>; sqlite: { schema: string; integrity: string }; counts: Record<string, number>; markdown: { raw_total: number; success: number; pending: number; failed: number; unsupported: number; latest_generated_at: string | null; recent_exports: { source_relpath: string; markdown_relpath: string; parse_engine: string; status: string; quality_score: number | null; updated_at: string | null }[] }; paths: { archive: string; markdown: string } }
type QueueCounts = { queued: number; running: number; completed: number; failed: number }
type ProcessingData = {
  queues: Record<"realtime_pending" | "realtime_done" | "historical_done_backfill", QueueCounts>
  historical_paused: boolean; historical_state: "paused" | "running" | "queued" | "idle"; mock_data: false
  gpu_leases: { resource: string; kind: string; owner: string; acquired_at: string; expires_at: string }[]
  tasks: { id: number; queue: string; stage: string; status: string; logical_item_key: string; title: string; progress_current: number; progress_total: number | null; attempts: number; error_code: string | null; recoverable: boolean; created_at: string }[]
}
type ProviderSettings = {
  agnes: { enabled: boolean; active_provider: "ollama" | "agnes"; ollama_base_url: string; ollama_model: string; agnes_base_url: string; agnes_model: string; provider_name: string; base_url: string; model: string; timeout_seconds: number; max_tokens: number; temperature: number; max_retries: number; max_concurrency: number; api_key_env: string; api_key_configured: boolean; agnes_api_key_configured: boolean; ollama_available: boolean; uses_local_gpu: boolean; real_oa_delivery_enabled: boolean; delivery_block_reason: string }
  feishu: { enabled: boolean; message_type: string; max_items_per_section: number; redact_confidential: boolean; retry_attempts: number; webhook_env: string; secret_env: string; webhook_configured: boolean; secret_configured: boolean }
}
type AuditData = {
  run: null | { id:number; status:string; total_items:number; completed_items:number; matched_items:number; mismatch_items:number; missing_download_items:number; local_extra_items:number; markdown_pending_items:number; access_failed_items:number; current_oa_item_key:string|null; started_at:string|null; finished_at:string|null }
  items: { id:number; oa_item_key:string; title:string; status:string; recognized_attachments:number|null; database_attachments:number; downloaded_attachments:number; markdown_attachments:number; error_code:string|null; error_detail:string|null; elapsed_seconds:number|null }[]
  errors: { error_code:string; count:number }[]
  events: { sequence:number; event_type:string; level:string; message:string; details:Record<string,unknown>; created_at:string|null }[]
  item_pagination: { page:number; page_size:number; total:number; pages:number }
  markdown_queue: { paused:boolean; discovered:number; queued:number; running:number; succeeded:number; failed:number; excluded:number; pdf_mineru:{paused:boolean;total:number;queued:number;running:number;succeeded:number;failed:number;skipped:number}; events:{id:number;event_type:string;level:string;message:string;created_at:string|null}[] }
  archive_dates: { total:number; dated:number; unknown:number; correct:number; pending:number; job:null|{id:number;status:string;processed?:number;total?:number;migrated?:number;failed?:number;last_error_code?:string|null} }
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
type AutoRunData = {
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

const nav = [
  { id: "pending" as View, label: "待处理", icon: FileCheck2 },
  { id: "done" as View, label: "已办归档", icon: Archive },
  { id: "processing" as View, label: "处理中心", icon: ListChecks },
  { id: "knowledge" as View, label: "知识库", icon: BookOpen },
  { id: "audit" as View, label: "审计", icon: ShieldCheck },
  { id: "system" as View, label: "系统", icon: Gauge },
  { id: "autorun" as View, label: "自动运行", icon: Bell },
]

async function api<T>(path: string): Promise<T> {
  const response = await fetch(path, { headers: { Accept: "application/json" } })
  const text = await response.text()
  if (!response.ok) throw new Error(`请求失败 (${response.status})`)
  try { return JSON.parse(text) as T } catch {
    // A non-JSON body on a 2xx response almost always means the request hit
    // the SPA fallback because the backend is an older build without this
    // route (e.g. /api/schedule/status). Tell the operator to restart the
    // backend rather than the unhelpful "请刷新服务" message.
    if (text.trimStart().startsWith("<")) {
      throw new Error("后端返回了网页而非接口数据，可能是后端版本未更新，请重启 oaradar-web 服务后重试")
    }
    throw new Error("服务返回了非 JSON 内容，请刷新服务")
  }
}
const time = (value: string | null) => value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "-"
const size = (value: number | null) => value == null ? "-" : value < 1024 * 1024 ? `${(value / 1024).toFixed(1)} KB` : `${(value / 1024 / 1024).toFixed(1)} MB`
const archiveStatus = (value: string) => ({ downloaded: "下载成功", no_attachment: "确认无附件", download_failed: "失败待处理", skipped: "规则跳过", processing: "处理中", pending_download: "待归档", archived: "归档完成", files_verified: "下载成功", parsed: "已解析" }[value] || value)
const archiveTone = (value: string): "good" | "warn" | "bad" | "neutral" => value === "download_failed" ? "bad" : ["downloaded", "archived", "files_verified", "parsed"].includes(value) ? "good" : ["processing", "pending_download"].includes(value) ? "warn" : "neutral"
const captureLabel = (value: string | null) => value === "pending_initial" ? "首次采集" : value === "pending_updated" ? "更新采集" : value || "已采集"

function Badge({ tone = "neutral", children }: { tone?: "good" | "warn" | "bad" | "info" | "neutral"; children: React.ReactNode }) {
  return <span className={`status status-${tone}`}>{children}</span>
}

export function App() {
  const [view, setView] = useState<View>("pending")
  const [mobileNav, setMobileNav] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const [query, setQuery] = useState("")
  const [pending, setPending] = useState<PendingItem[]>([])
  const [done, setDone] = useState<DoneItem[]>([])
  const [doneTotal, setDoneTotal] = useState(0)
  const [doneMetrics, setDoneMetrics] = useState<DoneMetrics>({ oa_done_total: 0, downloaded_items: 0, verified_attachments: 0 })
  const [donePage, setDonePage] = useState(1)
  const [syncing, setSyncing] = useState<"incremental" | "full" | null>(null)
  const [knowledge, setKnowledge] = useState<KnowledgeItem[]>([])
  const [system, setSystem] = useState<SystemData | null>(null)
  const [processing, setProcessing] = useState<ProcessingData | null>(null)
  const [providers, setProviders] = useState<ProviderSettings | null>(null)
  const [audit, setAudit] = useState<AuditData | null>(null)
  const [auditPage, setAuditPage] = useState(1)
  const [autorun, setAutoRun] = useState<AutoRunData | null>(null)
  const [pendingDetail, setPendingDetail] = useState<PendingDetail | null>(null)
  const [knowledgeDetail, setKnowledgeDetail] = useState<KnowledgeDetail | null>(null)

  const load = useCallback(async (silent = false) => {
    if (!silent) { setLoading(true); setError("") }
    try {
      if (view === "pending") setPending((await api<{ items: PendingItem[] }>("/api/lifecycle/pending")).items)
      if (view === "done") { const result = await api<{ items: DoneItem[]; total: number; metrics: DoneMetrics }>(`/api/lifecycle/done?page=${donePage}&page_size=100`); setDone(result.items); setDoneTotal(result.total); setDoneMetrics(result.metrics) }
      if (view === "processing") setProcessing(await api<ProcessingData>("/api/lifecycle/processing-center"))
      if (view === "knowledge") setKnowledge((await api<{ documents: KnowledgeItem[] }>("/api/lifecycle/knowledge")).documents)
      if (view === "audit") setAudit(await api<AuditData>(`/api/audits/online?item_page=${auditPage}&item_page_size=50`))
      if (view === "system") { const [status, providerSettings] = await Promise.all([api<SystemData>("/api/lifecycle/system"), api<ProviderSettings>("/api/system/provider-settings")]); setSystem(status); setProviders(providerSettings) }
      if (view === "autorun") setAutoRun(await api<AutoRunData>("/api/schedule/status"))
    } catch (reason) { setError(reason instanceof Error ? reason.message : "操作失败") }
    finally { if (!silent) setLoading(false) }
  }, [view, donePage, auditPage])
  useEffect(() => { void load() }, [load])
  useEffect(() => { if (view !== "audit") return; const timer = window.setInterval(() => void load(true), 3000); return () => window.clearInterval(timer) }, [view, load])

  const openPending = async (id: number) => {
    setError("")
    try { setPendingDetail(await api<PendingDetail>(`/api/lifecycle/pending/${id}`)) }
    catch (reason) { setError(reason instanceof Error ? reason.message : "详情加载失败") }
  }
  const openKnowledge = async (id: number) => {
    setError("")
    try { setKnowledgeDetail(await api<KnowledgeDetail>(`/api/lifecycle/knowledge/${id}`)) }
    catch (reason) { setError(reason instanceof Error ? reason.message : "详情加载失败") }
  }
  const startDoneSync = async (kind: "incremental" | "full") => {
    setSyncing(kind); setError("")
    try {
      const csrf = document.cookie.split("; ").find(row => row.startsWith("oa_csrf="))?.split("=")[1] || ""
      const response = await fetch(kind === "incremental" ? "/api/manifest/refresh-incremental" : "/api/manifest/start", { method: "POST", headers: { "x-csrf-token": csrf } })
      if (!response.ok) throw new Error(`启动同步失败 (${response.status})`)
    } catch (reason) { setError(reason instanceof Error ? reason.message : "启动同步失败") }
    finally { setSyncing(null); void load() }
  }
  const selectView = (next: View) => { setView(next); setQuery(""); setPendingDetail(null); setKnowledgeDetail(null); setMobileNav(false) }
  const filteredPending = pending.filter(item => `${item.title} ${item.sender ?? ""}`.toLowerCase().includes(query.toLowerCase()))
  const filteredDone = done.filter(item => `${item.title} ${item.sender ?? ""}`.toLowerCase().includes(query.toLowerCase()))
  const filteredKnowledge = knowledge.filter(item => item.title.toLowerCase().includes(query.toLowerCase()))

  return <div className="app-shell">
    <aside className={`sidebar ${mobileNav ? "sidebar-open" : ""}`}>
      <div className="brand"><span className="brand-mark">OA</span><div><strong>OARadar</strong><small>本地知识工作台</small></div></div>
      <nav aria-label="一级导航">{nav.map(item => <button key={item.id} className={`nav-item ${view === item.id ? "nav-active" : ""}`} onClick={() => selectView(item.id)}><item.icon size={18}/><span>{item.label}</span></button>)}</nav>
      <div className="privacy"><ShieldCheck size={16}/><span>只读连接<br/><small>OA 内容仅保存在本机</small></span></div>
    </aside>
    <main className="workspace">
      <header className="topbar">
        <button className="icon-button menu-button" title="打开导航" onClick={() => setMobileNav(!mobileNav)}><Menu size={19}/></button>
        <div><h1>{nav.find(item => item.id === view)?.label}</h1><p>{view === "pending" ? "当前 OA 待办及采集状态" : view === "done" ? "已完成事项的本地归档状态" : view === "processing" ? "实时流水线、历史补加工与 GPU 任务" : view === "knowledge" ? "通过质量门禁的知识文档" : "服务、队列与数据健康"}</p></div>
        <button className="icon-button refresh" title="刷新当前页面" onClick={() => void load()} disabled={loading}><RefreshCw size={18} className={loading ? "spin" : ""}/></button>
      </header>
      {error && <div className="error-banner"><CircleAlert size={18}/><span>{error}</span><button title="关闭" onClick={() => setError("")}><X size={17}/></button></div>}
      {loading ? <div className="loading"><Loader2 className="spin"/><span>正在读取本地状态</span></div> : <>
        {view === "pending" && <PendingView rows={filteredPending} all={pending} query={query} setQuery={setQuery} open={openPending}/>}
        {view === "done" && <DoneView rows={filteredDone} total={doneTotal} metrics={doneMetrics} page={donePage} setPage={setDonePage} query={query} setQuery={setQuery} syncing={syncing} startSync={startDoneSync}/>}
        {view === "processing" && processing && <ProcessingView data={processing}/>}
        {view === "knowledge" && <KnowledgeView rows={filteredKnowledge} query={query} setQuery={setQuery} open={openKnowledge}/>}
        {view === "audit" && audit && <AuditView data={audit} reload={load} setError={setError} setPage={setAuditPage}/>}
        {view === "system" && system && providers && <SystemView data={system} providers={providers} reload={load}/>}
        {view === "autorun" && autorun && <AutoRunView data={autorun} reload={load}/>}
      </>}
    </main>
    {pendingDetail && <PendingDrawer data={pendingDetail} close={() => setPendingDetail(null)}/>}
    {knowledgeDetail && <KnowledgeDrawer data={knowledgeDetail} close={() => setKnowledgeDetail(null)}/>}
  </div>
}

function ProcessingView({ data }: { data: ProcessingData }) {
  const labels: Record<string, string> = { realtime_pending: "实时新待办", realtime_done: "实时新已办", historical_done_backfill: "历史补加工" }
  const historyLabel = { paused: "已暂停", running: "运行中", queued: "等待中", idle: "待机" }[data.historical_state]
  return <section>
    <div className="queue-grid">{Object.entries(data.queues).map(([name, counts]) => <div className="queue-panel" key={name}><header><strong>{labels[name]}</strong>{name === "historical_done_backfill" && <Badge tone={data.historical_state === "paused" ? "warn" : data.historical_state === "idle" ? "neutral" : "good"}>{historyLabel}</Badge>}</header><div><Metric label="排队" value={counts.queued}/><Metric label="处理中" value={counts.running}/><Metric label="完成" value={counts.completed}/><Metric label="失败" value={counts.failed} bad={counts.failed > 0}/></div></div>)}</div>
    <div className="section-toolbar"><div><h2>GPU 资源</h2><p>MinerU 与本地 Ollama 使用数据库持久租约互斥。</p></div></div>
    {data.gpu_leases.length ? <div className="lease-strip">{data.gpu_leases.map(lease => <div key={lease.resource}><BrainCircuit size={17}/><span><strong>{lease.kind}</strong><small>{lease.owner} · 到期 {time(lease.expires_at)}</small></span></div>)}</div> : <div className="empty panel">当前没有 GPU 重型任务占用租约</div>}
    <div className="section-toolbar"><div><h2>最近任务</h2><p>状态直接来自持久队列，服务重启后继续保留。</p></div></div>
    <div className="table-wrap"><table><thead><tr><th>队列</th><th className="title-col">事项</th><th>阶段</th><th>状态</th><th>进度</th><th>尝试</th><th>错误码</th><th>创建时间</th></tr></thead><tbody>{data.tasks.map(task => <tr key={task.id}><td>{labels[task.queue] || task.queue}</td><td className="title-cell"><strong>{task.title}</strong><small>{task.logical_item_key}</small></td><td>{task.stage}</td><td><Badge tone={task.status === "failed" ? "bad" : task.status === "running" ? "warn" : task.status === "completed" ? "good" : "neutral"}>{task.status}</Badge></td><td>{task.progress_current}/{task.progress_total ?? "?"}</td><td>{task.attempts}</td><td>{task.error_code || "-"}</td><td className="nowrap">{time(task.created_at)}</td></tr>)}{!data.tasks.length && <tr><td colSpan={8} className="empty">队列尚未创建任务</td></tr>}</tbody></table></div>
  </section>
}

function SearchBox({ value, setValue, placeholder }: { value: string; setValue: (v: string) => void; placeholder: string }) {
  return <label className="search"><Search size={17}/><input value={value} onChange={e => setValue(e.target.value)} placeholder={placeholder}/>{value && <button title="清除搜索" onClick={() => setValue("")}><X size={15}/></button>}</label>
}
function PendingView({ rows, all, query, setQuery, open }: { rows: PendingItem[]; all: PendingItem[]; query: string; setQuery: (v: string) => void; open: (id: number) => void }) {
  const captured = all.filter(x => x.snapshot_id).length, attachments = all.reduce((n, x) => n + x.attachment_verified, 0), failures = all.reduce((n, x) => n + x.attachment_failed, 0)
  return <section>
    <div className="metrics compact-metrics"><Metric label="当前待办" value={all.length}/><Metric label="身份已采集" value={all.filter(x => x.identity_captured).length}/><Metric label="采集记录" value={captured}/><Metric label="附件已验证" value={attachments}/><Metric label="附件异常" value={failures} bad={failures > 0}/></div>
    <div className="section-toolbar"><div><h2>待办事项</h2><p>列表发现不等于归档完成，各证据阶段独立显示。</p></div><SearchBox value={query} setValue={setQuery} placeholder="搜索标题或发起人"/></div>
    <div className="table-wrap"><table style={{ minWidth: 1180 }}><thead><tr><th className="title-col">标题</th><th>发起人</th><th>当前节点</th><th>身份</th><th>采集记录</th><th>Ollama概括</th><th>附件</th><th>最近发现</th><th>最近摘要</th><th>最近同步</th><th>飞书通知</th><th>最近通知</th><th aria-label="详情"/></tr></thead><tbody>
      {rows.map(row => {
        const fs = row.feishu_status
        const fTone = fs == null ? "neutral" : fs === "sent" ? "good" : fs === "failed" ? "bad" : "warn"
        const fLabel = fs == null ? "未通知" : ({ sent: "已发送", failed: "失败", queued: "待发送", pending: "待发送", retry_wait: "重试中", unknown: "未知" } as Record<string, string>)[fs] || fs
        return <tr key={row.id} onClick={() => open(row.id)} tabIndex={0} onKeyDown={e => e.key === "Enter" && open(row.id)}><td className="title-cell"><strong>{row.title}</strong><small>逻辑事项 #{row.logical_item_id}</small></td><td>{row.sender || "-"}</td><td>{row.current_node || "-"}</td><td><Badge tone={row.identity_captured ? "good" : "warn"}>{row.identity_captured ? "已采集" : "待采集"}</Badge></td><td><Badge tone={row.snapshot_id ? "good" : "neutral"}>{row.snapshot_id ? captureLabel(row.snapshot_kind) : "未采集"}</Badge></td><td><Badge tone={row.ollama_summary_status === "current" ? "good" : "neutral"}>{row.ollama_summary_status === "current" ? "已生成" : "待生成"}</Badge></td><td><Badge tone={row.attachment_failed ? "bad" : row.attachment_verified ? "good" : "neutral"}>{row.attachment_verified}/{row.attachment_total}{row.attachment_failed ? ` · 失败 ${row.attachment_failed}` : ""}</Badge></td><td className="nowrap">{time(row.last_discovered_at)}</td><td className="nowrap">{time(row.last_summary_at)}</td><td className="nowrap">{time(row.last_synced_at)}</td><td><Badge tone={fTone}>{fLabel}</Badge>{row.notify_error_code && <small className="cell-error">{row.notify_error_code}</small>}</td><td className="nowrap">{time(row.last_notified_at)}</td><td><ChevronRight size={17}/></td></tr>
      })}
      {!rows.length && <tr><td colSpan={9} className="empty">没有符合条件的待办事项</td></tr>}
    </tbody></table></div>
  </section>
}
function DoneView({ rows, total, metrics, page, setPage, query, setQuery, syncing, startSync }: { rows: DoneItem[]; total: number; metrics: DoneMetrics; page: number; setPage: (page: number) => void; query: string; setQuery: (v: string) => void; syncing: "incremental" | "full" | null; startSync: (kind: "incremental" | "full") => void }) {
  const pages = Math.max(1, Math.ceil(total / 100))
  return <section><div className="metrics"><Metric label="OA已办事项" value={metrics.oa_done_total}/><Metric label="成功下载事项" value={metrics.downloaded_items}/><Metric label="已验证附件" value={metrics.verified_attachments}/></div>
    <div className="notice"><CircleAlert size={17}/><span>待办到已办的稳定主键 Pilot 已验证；系统使用事项ID关联，不按标题自动合并。</span></div>
    <div className="section-toolbar"><div><h2>已办归档</h2><p>列表分页显示；顶部统计覆盖全部已办事项。</p></div><div className="toolbar-actions"><button disabled={syncing !== null} onClick={() => startSync("incremental")}><RefreshCw size={16} className={syncing === "incremental" ? "spin" : ""}/>增量刷新</button><button disabled={syncing !== null} onClick={() => startSync("full")}><Database size={16} className={syncing === "full" ? "spin" : ""}/>全量核对</button><SearchBox value={query} setValue={setQuery} placeholder="搜索当前页"/></div></div>
    <div className="table-wrap"><table><thead><tr><th className="title-col">标题</th><th>发起人</th><th>办理时间</th><th>归档状态</th><th>附件数</th><th>本地目录</th></tr></thead><tbody>{rows.map(row => <tr key={row.id}><td className="title-cell"><strong>{row.title}</strong><small>OA事项ID {row.item_id}</small></td><td>{row.sender || "-"}</td><td className="nowrap">{time(row.completed_at)}</td><td><Badge tone={archiveTone(row.pipeline_status)}>{archiveStatus(row.pipeline_status)}</Badge></td><td>{row.file_count == null ? "待归档" : row.file_count}</td><td className="path-cell" title={row.archive_relpath || ""}>{row.archive_relpath || "-"}</td></tr>)}</tbody></table></div>
    <div className="pagination"><button disabled={page <= 1} onClick={() => setPage(page - 1)}>上一页</button><span>第 {page}/{pages} 页</span><button disabled={page >= pages} onClick={() => setPage(page + 1)}>下一页</button></div>
  </section>
}
function KnowledgeView({ rows, query, setQuery, open }: { rows: KnowledgeItem[]; query: string; setQuery: (v: string) => void; open: (id: number) => void }) {
  return <section><div className="metrics"><Metric label="知识文档" value={rows.length}/><Metric label="有效解析产物" value={rows.filter(x => x.active_parse_artifact_id).length}/><Metric label="来源关系" value={rows.reduce((n, x) => n + x.source_count, 0)}/></div>
    <div className="section-toolbar"><div><h2>知识文档</h2><p>一份内容一个知识正文，保留所有 OA 与附件来源。</p></div><SearchBox value={query} setValue={setQuery} placeholder="搜索知识文档"/></div>
    <div className="document-grid">{rows.map(row => <button className="document-row" key={row.id} onClick={() => open(row.id)}><FileText size={21}/><span><strong>{row.title}</strong><small>{row.vault_relpath}</small></span><Badge tone={row.publish_status.includes("draft") ? "warn" : "good"}>{row.publish_status}</Badge><span className="source-count">{row.source_count} 个来源</span><ChevronRight size={18}/></button>)}{!rows.length && <div className="empty panel">尚无符合条件的知识文档</div>}</div>
  </section>
}
function AuditView({ data, reload, setError, setPage }: { data: AuditData; reload: () => Promise<void>; setError: (value:string)=>void; setPage:(page:number)=>void }) {
  const [acting, setActing] = useState(false)
  const run = data.run
  const action = async (kind: "start" | "pause" | "resume") => {
    setActing(true); setError("")
    try {
      const csrf = document.cookie.split("; ").find(row => row.startsWith("oa_csrf="))?.split("=")[1] || ""
      const path = kind === "start" ? "/api/audits/online" : `/api/audits/online/${run?.id}/${kind}`
      const response = await fetch(path, { method:"POST", headers:{"x-csrf-token":csrf} })
      if (!response.ok) throw new Error(`审计操作失败 (${response.status})`)
      await reload()
    } catch (reason) { setError(reason instanceof Error ? reason.message : "审计操作失败") }
    finally { setActing(false) }
  }
  const progress = run?.total_items ? Math.round(run.completed_items / run.total_items * 100) : 0
  const current = data.items.find(item => item.oa_item_key === run?.current_oa_item_key)
  const mdAction = async (kind:"pause"|"resume"|"retry-failed") => { setActing(true); try { const csrf=document.cookie.split("; ").find(row=>row.startsWith("oa_csrf="))?.split("=")[1]||""; const response=await fetch(`/api/audits/markdown/${kind}`,{method:"POST",headers:{"x-csrf-token":csrf}}); if(!response.ok) throw new Error(`MD 化操作失败 (${response.status})`); await reload() } catch(reason) { setError(reason instanceof Error?reason.message:"MD 化操作失败") } finally { setActing(false) } }
  const pdfAction = async (kind:"start"|"pause"|"resume"|"retry-failed") => { setActing(true); try { const csrf=document.cookie.split("; ").find(row=>row.startsWith("oa_csrf="))?.split("=")[1]||""; const path=kind==="start"?"/api/audits/pdf-mineru":`/api/audits/pdf-mineru/${kind}`; const response=await fetch(path,{method:"POST",headers:{"x-csrf-token":csrf}}); if(!response.ok) throw new Error(`PDF重转操作失败 (${response.status})`); await reload() } catch(reason) { setError(reason instanceof Error?reason.message:"PDF重转操作失败") } finally { setActing(false) } }
  const dateAction = async (kind:"start"|"pause"|"resume") => { setActing(true); try { const csrf=document.cookie.split("; ").find(row=>row.startsWith("oa_csrf="))?.split("=")[1]||""; const path=kind==="start"?"/api/audits/archive-dates":`/api/audits/archive-dates/${kind}`; const response=await fetch(path,{method:"POST",headers:{"x-csrf-token":csrf}}); if(!response.ok) throw new Error(`归档校准操作失败 (${response.status})`); await reload() } catch(reason) { setError(reason instanceof Error?reason.message:"归档校准操作失败") } finally { setActing(false) } }
  return <section>
    <div className="audit-hero"><div><span className="eyebrow">只读在线复查</span><h2>已办事项审计</h2><p>逐条访问 OA 详情，核对附件识别、成功下载与 Markdown 转换结果。</p></div><div className="audit-actions">{(!run || ["completed","failed"].includes(run.status)) && <button className="button-primary" disabled={acting} onClick={()=>void action("start")}><ShieldCheck size={16}/>开始在线审计</button>}{run && ["queued","running"].includes(run.status) && <button className="button-danger" disabled={acting} onClick={()=>void action("pause")}><CircleAlert size={16}/>暂停</button>}{run && ["paused","pause_requested"].includes(run.status) && <button className="button-primary" disabled={acting} onClick={()=>void action("resume")}><RefreshCw size={16}/>继续审计</button>}</div></div>
    {run ? <><div className="audit-progress"><div><strong>{run.completed_items.toLocaleString()} / {run.total_items.toLocaleString()}</strong><span>{progress}% · {run.status}</span></div><Progress value={progress} label="在线审计进度"/>{current && <p>当前事项：<strong>{current.title}</strong> <small>{current.oa_item_key}</small></p>}</div>
    <div className="metrics compact-metrics"><Metric label="已复查" value={run.completed_items}/><Metric label="附件一致" value={run.matched_items}/><Metric label="附件缺失" value={run.missing_download_items} bad={run.missing_download_items>0}/><Metric label="历史附件留存" value={run.local_extra_items}/><Metric label="待MD化" value={run.markdown_pending_items}/><Metric label="OA访问失败" value={run.access_failed_items} bad={run.access_failed_items>0}/><Metric label="剩余" value={Math.max(run.total_items-run.completed_items,0)}/></div>
    <div className="audit-hero"><div><span className="eyebrow">独立本地进程</span><h2>Markdown 化流水线</h2><p>只处理本地已验证附件，不访问 OA；与在线扫描并行推进。</p></div><div className="audit-actions">{data.markdown_queue.paused?<button className="button-primary" disabled={acting} onClick={()=>void mdAction("resume")}><RefreshCw size={16}/>继续 MD 化</button>:<button className="button-danger" disabled={acting} onClick={()=>void mdAction("pause")}><CircleAlert size={16}/>暂停 MD 化</button>}<button disabled={acting||!data.markdown_queue.failed} onClick={()=>void mdAction("retry-failed")}><RefreshCw size={16}/>重试失败</button></div></div>
    <div className="metrics compact-metrics"><Metric label="附件任务" value={data.markdown_queue.discovered}/><Metric label="排队中" value={data.markdown_queue.queued}/><Metric label="转换中" value={data.markdown_queue.running}/><Metric label="转换成功" value={data.markdown_queue.succeeded}/><Metric label="已排除非附件" value={data.markdown_queue.excluded}/><Metric label="转换失败" value={data.markdown_queue.failed} bad={data.markdown_queue.failed>0}/></div>
    <div className="panel audit-log">{data.markdown_queue.events.map(event=><div className={`log-${event.level}`} key={event.id}><time>{time(event.created_at)}</time><span>{event.message}</span><Badge tone={event.level==="error"?"bad":"neutral"}>{event.event_type}</Badge></div>)}</div>
    <div className="audit-hero"><div><span className="eyebrow">GPU 优先队列</span><h2>PDF MinerU 重转</h2><p>将没有 MinerU 成功成果的已验证 PDF 原子替换为 MinerU 版本。</p></div><div className="audit-actions"><button disabled={acting||data.markdown_queue.pdf_mineru.queued>0||data.markdown_queue.pdf_mineru.running>0} onClick={()=>void pdfAction("start")}><ShieldCheck size={16}/>启动扫描</button>{data.markdown_queue.pdf_mineru.paused?<button className="button-primary" disabled={acting} onClick={()=>void pdfAction("resume")}><RefreshCw size={16}/>继续重转</button>:<button className="button-danger" disabled={acting} onClick={()=>void pdfAction("pause")}><CircleAlert size={16}/>暂停重转</button>}<button disabled={acting||!data.markdown_queue.pdf_mineru.failed} onClick={()=>void pdfAction("retry-failed")}><RefreshCw size={16}/>重试失败</button></div></div>
    <div className="metrics compact-metrics"><Metric label="PDF总数" value={data.markdown_queue.pdf_mineru.total}/><Metric label="待重转" value={data.markdown_queue.pdf_mineru.queued}/><Metric label="转换中" value={data.markdown_queue.pdf_mineru.running}/><Metric label="MinerU成功" value={data.markdown_queue.pdf_mineru.succeeded}/><Metric label="已跳过" value={data.markdown_queue.pdf_mineru.skipped}/><Metric label="失败" value={data.markdown_queue.pdf_mineru.failed} bad={data.markdown_queue.pdf_mineru.failed>0}/></div>
    <div className="audit-hero"><div><span className="eyebrow">本地原子迁移</span><h2>发起时间归档校准</h2><p>按 OA 发起时间重排原始文件和 Markdown；无日期事项进入 unknown。</p></div><div className="audit-actions">{data.archive_dates.job?.status==="paused"?<button className="button-primary" disabled={acting} onClick={()=>void dateAction("resume")}><RefreshCw size={16}/>继续校准</button>:data.archive_dates.job?.status==="running"?<button className="button-danger" disabled={acting} onClick={()=>void dateAction("pause")}><CircleAlert size={16}/>暂停校准</button>:<button className="button-primary" disabled={acting||data.archive_dates.pending===0} onClick={()=>void dateAction("start")}><Archive size={16}/>开始校准</button>}</div></div>
    <div className="metrics compact-metrics"><Metric label="已归档事项" value={data.archive_dates.total}/><Metric label="有发起时间" value={data.archive_dates.dated}/><Metric label="时间未知" value={data.archive_dates.unknown} bad={data.archive_dates.unknown>0}/><Metric label="位置正确" value={data.archive_dates.correct}/><Metric label="待迁移" value={data.archive_dates.pending}/><Metric label="迁移失败" value={data.archive_dates.job?.failed||0} bad={(data.archive_dates.job?.failed||0)>0}/></div>
    <div className="audit-lower"><div><div className="section-toolbar"><div><h2>OA 错误汇总</h2><p>仅显示脱敏后的稳定错误码。</p></div></div><div className="panel audit-errors">{data.errors.length?data.errors.map(row=><div key={row.error_code}><code>{row.error_code}</code><strong>{row.count}</strong></div>):<p>暂无 OA 访问错误</p>}</div></div><div><div className="section-toolbar"><div><h2>实施日志</h2><p>最近 200 条持久化结构化事件。</p></div></div><div className="panel audit-log">{data.events.map(event=><div className={`log-${event.level}`} key={event.sequence}><time>{time(event.created_at)}</time><span>{event.message}</span><Badge tone={event.level==="error"?"bad":event.level==="warning"?"warn":"neutral"}>{event.event_type}</Badge></div>)}</div></div></div>
    <div className="section-toolbar"><div><h2>事项复查结果</h2><p>共 {data.item_pagination.total.toLocaleString()} 项，每页 {data.item_pagination.page_size} 项。</p></div><div className="pagination"><button disabled={data.item_pagination.page<=1} onClick={()=>setPage(data.item_pagination.page-1)}>上一页</button><span>第 {data.item_pagination.page} / {Math.max(data.item_pagination.pages,1)} 页</span><button disabled={data.item_pagination.page>=data.item_pagination.pages} onClick={()=>setPage(data.item_pagination.page+1)}>下一页</button></div></div>
    <div className="table-wrap"><table><thead><tr><th className="title-col">OA事项</th><th>OA识别</th><th>数据库</th><th>已下载</th><th>MD化</th><th>耗时</th><th>状态</th><th>错误</th></tr></thead><tbody>{data.items.map(item=><tr key={item.id}><td className="title-cell"><strong>{item.title}</strong><small>{item.oa_item_key}</small></td><td>{item.recognized_attachments ?? "-"}</td><td>{item.database_attachments}</td><td>{item.downloaded_attachments}</td><td>{item.markdown_attachments}</td><td>{item.elapsed_seconds == null ? "-" : `${item.elapsed_seconds.toFixed(2)}s`}</td><td><Badge tone={item.status==="matched"?"good":item.status==="access_failed"?"bad":["missing_download","local_extra"].includes(item.status)?"warn":"neutral"}>{item.status}</Badge></td><td title={item.error_detail||""}>{item.error_code||"-"}</td></tr>)}{!data.items.length&&<tr><td colSpan={8} className="empty">本页没有事项</td></tr>}</tbody></table></div></>:<div className="empty panel">尚未创建在线审计。点击“开始在线审计”后，后台 Worker 将以只读方式遍历全部已办事项。</div>}
  </section>
}
function SystemView({ data, providers, reload }: { data: SystemData; providers: ProviderSettings; reload: () => Promise<void> }) {
  const mineruStatus = String(data.mineru.status ?? "unknown")
  const workerDetail = data.worker ? `${data.worker.type} · ${data.worker.progress_current}/${data.worker.progress_total ?? "?"} · 失败 ${data.worker.failure_count}` : "当前无运行任务"
  const [form, setForm] = useState(providers)
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState("")
  const save = async () => {
    setSaving(true); setMessage("")
    try {
      const csrf = document.cookie.split("; ").find(row => row.startsWith("oa_csrf="))?.split("=")[1] || ""
      const response = await fetch("/api/system/provider-settings", { method: "PATCH", headers: { "Content-Type": "application/json", "x-csrf-token": csrf }, body: JSON.stringify({
        agnes: { enabled: form.agnes.enabled, active_provider: form.agnes.active_provider, ollama_base_url: form.agnes.ollama_base_url, ollama_model: form.agnes.ollama_model, agnes_base_url: form.agnes.agnes_base_url, agnes_model: form.agnes.agnes_model, timeout_seconds: Number(form.agnes.timeout_seconds), max_tokens: Number(form.agnes.max_tokens), temperature: Number(form.agnes.temperature), max_retries: Number(form.agnes.max_retries), max_concurrency: Number(form.agnes.max_concurrency) },
        feishu: { enabled: form.feishu.enabled, message_type: form.feishu.message_type, max_items_per_section: Number(form.feishu.max_items_per_section), redact_confidential: form.feishu.redact_confidential, retry_attempts: Number(form.feishu.retry_attempts) },
      }) })
      if (!response.ok) throw new Error(`保存失败 (${response.status})`)
      setMessage("设置已保存，需要重启服务后生效")
      await reload()
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : "保存失败") }
    finally { setSaving(false) }
  }
  return <section><div className="service-grid"><Service icon={Server} title="Web 服务" status={data.web.status} detail={data.web.url}/><Service icon={Gauge} title="Worker" status={data.worker?.status || "idle"} detail={workerDetail}/><Service icon={HardDrive} title="MinerU" status={mineruStatus} detail="127.0.0.1:58000"/><Service icon={Database} title="SQLite" status={data.sqlite.integrity === "ok" ? "healthy" : data.sqlite.integrity} detail={`Schema ${data.sqlite.schema}`}/></div>
    {data.worker && <div className="current-job"><div><span>当前事项</span><strong>{data.worker.current_title || "正在准备"}</strong></div><div><span>全部进度</span><strong>{data.worker.progress_current}/{data.worker.progress_total ?? "?"}</strong></div><div><span>当前附件</span><strong>{data.worker.attachment_verified}/{data.worker.attachment_total}</strong></div><div className={data.worker.failure_count ? "job-failed" : ""}><span>失败</span><strong>{data.worker.failure_count}</strong></div></div>}
    <div className="section-toolbar"><div><h2>Markdown 转换</h2><p title={data.paths.archive}>原始目录：{data.paths.archive}<br/>输出目录：{data.paths.markdown}</p></div></div><div className="metrics compact-metrics"><Metric label="原始文件" value={data.markdown.raw_total}/><Metric label="已转换" value={data.markdown.success}/><Metric label="待转换" value={data.markdown.pending}/><Metric label="不支持" value={data.markdown.unsupported}/><Metric label="转换失败" value={data.markdown.failed} bad={data.markdown.failed > 0}/></div>
    <div className="table-wrap"><table><thead><tr><th className="title-col">原始文件</th><th className="title-col">Markdown</th><th>解析引擎</th><th>质量</th><th>状态</th><th>更新时间</th></tr></thead><tbody>{data.markdown.recent_exports.map((row, index) => <tr key={`${row.markdown_relpath}-${index}`}><td className="path-cell" title={row.source_relpath}>{row.source_relpath}</td><td className="path-cell" title={row.markdown_relpath}>{row.markdown_relpath}</td><td>{row.parse_engine}</td><td>{row.quality_score == null ? "-" : row.quality_score.toFixed(2)}</td><td><Badge tone={row.status === "success" ? "good" : row.status === "failed" ? "bad" : "warn"}>{row.status}</Badge></td><td className="nowrap">{time(row.updated_at)}</td></tr>)}{!data.markdown.recent_exports.length && <tr><td colSpan={6} className="empty">尚无 Markdown 转换记录，请先运行 oa convert</td></tr>}</tbody></table></div>
    <div className="section-toolbar"><div><h2>数据状态</h2><p>当前数据库中的生命周期、文件与知识对象。</p></div></div><div className="data-grid">{Object.entries(data.counts).map(([key, value]) => <div className={key === "failed_or_retry" && value ? "data-cell data-cell-bad" : "data-cell"} key={key}><span>{labelFor(key)}</span><strong>{value.toLocaleString()}</strong></div>)}</div>
    <div className="settings-head"><div><h2>模型与通知</h2><p>凭证仅从环境变量读取，页面不会显示明文。</p></div><button className="button-primary" onClick={() => void save()} disabled={saving}><Save size={16}/>{saving ? "保存中" : "保存设置"}</button></div>
    {message && <div className="settings-message">{message}</div>}
    <div className="settings-grid">
      <div className="settings-panel"><h3><BrainCircuit size={18}/>摘要模型</h3><Toggle label="启用模型摘要" checked={form.agnes.enabled} change={v => setForm({...form, agnes:{...form.agnes,enabled:v}})}/><div className="provider-choice"><button className={form.agnes.active_provider==="ollama"?"provider-active":""} onClick={()=>setForm({...form,agnes:{...form.agnes,active_provider:"ollama"}})}><strong>本地 Ollama</strong><small>首选 · qwen3.5:9b</small></button><button className={form.agnes.active_provider==="agnes"?"provider-active":""} onClick={()=>setForm({...form,agnes:{...form.agnes,active_provider:"agnes"}})}><strong>Agnes</strong><small>备用 · agnes-2.0-flash</small></button></div>{form.agnes.active_provider==="ollama"?<><Field label="Ollama API 地址" value={form.agnes.ollama_base_url} change={v => setForm({...form,agnes:{...form.agnes,ollama_base_url:v}})}/><Field label="Ollama 模型" value={form.agnes.ollama_model} change={v => setForm({...form,agnes:{...form.agnes,ollama_model:v}})}/><SecretState label="Docker Ollama" configured={form.agnes.ollama_available}/><div className="privacy-note"><ShieldCheck size={15}/>本地处理；与 MinerU 共用 GPU，任务自动串行</div></>:<><Field label="Agnes API 地址" value={form.agnes.agnes_base_url} change={v => setForm({...form,agnes:{...form.agnes,agnes_base_url:v}})}/><Field label="Agnes 模型" value={form.agnes.agnes_model} change={v => setForm({...form,agnes:{...form.agnes,agnes_model:v}})}/><SecretState label="AGNES_API_KEY" configured={form.agnes.agnes_api_key_configured}/><div className="privacy-note"><ShieldCheck size={15}/>远程真实 OA 内容外发保持关闭</div></>}<div className="field-pair"><NumberField label="超时（秒）" value={form.agnes.timeout_seconds} change={v => setForm({...form,agnes:{...form.agnes,timeout_seconds:v}})}/><NumberField label="最大输出" value={form.agnes.max_tokens} change={v => setForm({...form,agnes:{...form.agnes,max_tokens:v}})}/><NumberField label="温度" value={form.agnes.temperature} step="0.1" change={v => setForm({...form,agnes:{...form.agnes,temperature:v}})}/><NumberField label="并发" value={form.agnes.max_concurrency} change={v => setForm({...form,agnes:{...form.agnes,max_concurrency:v}})}/></div></div>
      <div className="settings-panel"><h3><Bell size={18}/>飞书通知</h3><Toggle label="启用飞书" checked={form.feishu.enabled} change={v => setForm({...form,feishu:{...form.feishu,enabled:v}})}/><Field label="消息类型" value={form.feishu.message_type} change={v => setForm({...form,feishu:{...form.feishu,message_type:v}})}/><div className="field-pair"><NumberField label="每次最多事项" value={form.feishu.max_items_per_section} change={v => setForm({...form,feishu:{...form.feishu,max_items_per_section:v}})}/><NumberField label="重试次数" value={form.feishu.retry_attempts} change={v => setForm({...form,feishu:{...form.feishu,retry_attempts:v}})}/></div><Toggle label="通知内容脱敏" checked={form.feishu.redact_confidential} change={v => setForm({...form,feishu:{...form.feishu,redact_confidential:v}})}/><SecretState label={form.feishu.webhook_env} configured={form.feishu.webhook_configured}/><SecretState label={form.feishu.secret_env} configured={form.feishu.secret_configured}/></div>
    </div>
  </section>
}
function ServiceCard({ title, svc }: { title: string; svc: ServiceStatus }) {
  return <div className={`service-card ${svc.active ? "service-on" : svc.installed ? "service-idle" : "service-off"}`}>
    <header><strong>{title}</strong>{svc.active ? <Badge tone="good">运行中</Badge> : svc.installed ? <Badge tone="warn">未运行</Badge> : <Badge tone="neutral">未安装</Badge>}</header>
    <div className="service-flags">
      <span>安装 <Badge tone={svc.installed ? "good" : "neutral"}>{svc.installed ? "是" : "否"}</Badge></span>
      <span>启用 <Badge tone={svc.enabled ? "good" : "neutral"}>{svc.enabled ? "是" : "否"}</Badge></span>
    </div>
    <dl>
      <div><dt>最近启动</dt><dd>{time(svc.last_started_at)}</dd></div>
      <div><dt>下次运行</dt><dd>{svc.next_run_at || "—"}</dd></div>
      <div><dt>最近错误</dt><dd className={svc.last_error ? "bad-text" : ""}>{svc.last_error || "无"}</dd></div>
    </dl>
  </div>
}

const OVERALL_TONE: Record<string, "good" | "warn" | "bad" | "neutral"> = {
  正常: "good", 未安装: "warn", 已暂停: "warn", 登录失效: "bad", 配置异常: "bad",
}

function AutoRunView({ data, reload }: { data: AutoRunData; reload: () => Promise<void> }) {
  const [acting, setActing] = useState("")
  const [message, setMessage] = useState("")
  const [confirming, setConfirming] = useState<null | { label: string; action: string }>(null)
  const [activeJob, setActiveJob] = useState<JobStatus | null>(null)

  // Plan §6.5: auto-refresh the panel every 5s.
  useEffect(() => {
    const timer = window.setInterval(() => void reload(), 5000)
    return () => window.clearInterval(timer)
  }, [reload])

  // Plan §6.4: poll the live job every 2s until it leaves the running state.
  useEffect(() => {
    if (!activeJob) return
    if (!["queued", "running"].includes(activeJob.status)) return
    const timer = window.setInterval(async () => {
      try {
        const res = await fetch(`/api/schedule/job/${activeJob.job_id}`, { headers: { Accept: "application/json" } })
        if (res.ok) {
          const next = await res.json() as JobStatus
          setActiveJob(next)
          if (!["queued", "running"].includes(next.status)) void reload()
        }
      } catch { /* transient; keep polling */ }
    }, 2000)
    return () => window.clearInterval(timer)
  }, [activeJob, reload])

  const csrfToken = () => document.cookie.split("; ").find(row => row.startsWith("oa_csrf="))?.split("=")[1] || ""

  const runPath = async (path: string, label: string, body?: object) => {
    setActing(label); setMessage("")
    try {
      const headers: Record<string, string> = { "x-csrf-token": csrfToken() }
      if (body) headers["Content-Type"] = "application/json"
      const response = await fetch(path, { method: "POST", headers, body: body ? JSON.stringify(body) : undefined })
      if (!response.ok) {
        const detail = await response.text().catch(() => "")
        throw new Error(`${label}失败 (${response.status})${detail ? `: ${detail}` : ""}`)
      }
      if (path.endsWith("/schedule/hourly") || path.endsWith("/schedule/nightly")) {
        const job = await response.json() as { job_id: number; status: string; stage: string }
        setActiveJob({ job_id: job.job_id, found: true, status: job.status, stage: job.stage, progress_current: 0, progress_total: null, last_error_code: null, started_at: null, finished_at: null, current_event: null, run: null })
      } else {
        setMessage(`${label}已触发`)
      }
      await reload()
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : `${label}失败`) }
    finally { setActing("") }
  }

  const controls: { label: string; action: string; danger: boolean }[] = [
    { label: "立即扫描", action: "hourly", danger: false },
    { label: "立即夜间补齐", action: "nightly", danger: false },
    { label: "测试飞书", action: "test", danger: false },
    { label: "安装自动运行", action: "install", danger: true },
    { label: "启用自动运行", action: "enable", danger: true },
    { label: "暂停自动运行", action: "disable", danger: true },
    { label: "重启Worker", action: "restart_worker", danger: true },
    { label: "重新登录OA", action: "relogin", danger: true },
  ]
  const onControl = (c: { label: string; action: string; danger: boolean }) => {
    if (c.danger) { setConfirming(c); return }
    void dispatch(c)
  }
  const dispatch = (c: { label: string; action: string }) => {
    if (c.action === "hourly") return void runPath("/api/schedule/hourly", c.label)
    if (c.action === "nightly") return void runPath("/api/schedule/nightly", c.label)
    if (c.action === "test") return void runPath("/api/notifications/test", c.label)
    return void runPath("/api/schedule/control", c.label, { action: c.action })
  }

  const s = data.summary
  const overall = data.overall_status
  const overallTone = OVERALL_TONE[overall] || "neutral"
  const loginLabel = { authenticated: "已登录", unknown: "未知" }[s.oa_login.status] || s.oa_login.status
  const serviceTitles: [keyof typeof data.services, string][] = [
    ["web", "Web 服务"], ["worker", "OA Worker"], ["markdown_worker", "Markdown Worker"],
    ["hourly_timer", "每小时定时器"], ["nightly_timer", "夜间定时器"],
  ]

  return <section>
    <div className={`overall-banner status-${overallTone}`}>
      <strong>自动运行总状态：{overall}</strong>
      <span className="overall-meta">构建 {data.system_info.git_commit || "?"}{data.system_info.build_time ? ` · ${time(data.system_info.build_time)}` : ""}</span>
    </div>
    <div className="notice"><CircleAlert size={17}/><span>定时扫描与飞书通知由本机 systemd 定时器或手动触发执行；所有操作均为只读 OA 交互，不修改 OA 记录。</span></div>

    <div className="section-toolbar"><div><h2>服务状态</h2><p>五项独立服务：是否安装 / 启用 / 运行 / 最近启动 / 最近错误 / 下次运行（plan §6.2）。</p></div></div>
    <div className="service-grid-5">
      {serviceTitles.map(([key, title]) => <ServiceCard key={key} title={title} svc={data.services[key]} />)}
    </div>

    {activeJob && activeJob.found && <div className="job-progress">
      <div className="section-toolbar"><div><h2>手动扫描进度</h2><p>任务 #{activeJob.job_id} · {activeJob.stage} · <Badge tone={activeJob.status === "completed" ? "good" : activeJob.status === "failed" || activeJob.status === "auth_required" ? "bad" : "warn"}>{activeJob.status}</Badge></p></div></div>
      <div className="metrics compact-metrics">
        <Metric label="当前阶段" value={activeJob.current_event || activeJob.status}/>
        <Metric label="进度" value={`${activeJob.progress_current}/${activeJob.progress_total ?? "?"}`}/>
        <Metric label="待办扫描" value={activeJob.run?.summary?.pending?.source_total ?? "-"} />
        <Metric label="待办新增" value={activeJob.run?.summary?.pending?.created ?? "-"} />
        <Metric label="已办新增" value={activeJob.run?.summary?.done?.new_items ?? "-"} />
        <Metric label="开始" value={time(activeJob.started_at)}/>
        <Metric label="结束" value={time(activeJob.finished_at)}/>
      </div>
      {activeJob.last_error_code && <div className="error-banner"><CircleAlert size={18}/><span>错误码：{activeJob.last_error_code}</span></div>}
    </div>}

    <div className="section-toolbar"><div><h2>控制</h2><p>危险操作（安装 / 启用 / 暂停 / 重启 / 重新登录）需要二次确认。</p></div>
      <div className="toolbar-actions">
        {controls.map(c => <button key={c.action} disabled={acting !== ""} className={c.danger ? "button-danger" : ""} onClick={() => onControl(c)}>
          {c.action === "hourly" && <RefreshCw size={16} className={acting === c.label ? "spin" : ""}/>}
          {c.action === "nightly" && <Database size={16} className={acting === c.label ? "spin" : ""}/>}
          {c.action === "test" && <Bell size={16} className={acting === c.label ? "spin" : ""}/>}
          {c.label}
        </button>)}
      </div>
    </div>
    {message && <div className="settings-message">{message}</div>}

    <div className="metrics compact-metrics">
      <Metric label="每小时扫描" value={data.hourly_enabled === null ? "未知" : data.hourly_enabled ? "已启用" : "未启用"}/>
      <Metric label="下次运行" value={data.next_run_at || "未知"}/>
      <Metric label="最近扫描" value={time(data.last_scan_at)}/>
      <Metric label="待办新增" value={s.pending_new}/>
      <Metric label="待办变化" value={s.pending_changed}/>
      <Metric label="已办新增" value={s.done_new}/>
      <Metric label="OA 登录" value={loginLabel} bad={s.oa_login.status !== "authenticated"}/>
      <Metric label="飞书成功" value={s.feishu.sent}/>
      <Metric label="飞书失败" value={s.feishu.failed} bad={s.feishu.failed > 0}/>
      <Metric label="MD 队列积压" value={s.markdown_backlog} bad={s.markdown_backlog > 0}/>
    </div>
    <div className="section-toolbar"><div><h2>夜间补齐结果</h2><p>最近一次夜间同步的归档与知识库入队情况。</p></div></div>
    <div className="metrics compact-metrics">
      <Metric label="夜间运行" value={time(s.nightly.last_at)}/>
      <Metric label="下载任务入队" value={s.nightly.download_jobs_enqueued}/>
      <Metric label="Markdown 任务入队" value={s.nightly.markdown_tasks_enqueued}/>
      <Metric label="飞书状态" value={s.feishu.state}/>
    </div>

    <div className="section-toolbar"><div><h2>运行记录</h2><p>最近 {data.recent_runs.length} 条定时运行（bootstrap / hourly / nightly）。</p></div></div>
    <div className="table-wrap"><table><thead><tr><th>阶段</th><th>状态</th><th>开始</th><th>结束</th><th>待办新增</th><th>已办新增</th><th>MD 入队</th></tr></thead><tbody>
      {data.recent_runs.map(run => {
        const sum = run.summary || {}
        return <tr key={run.run_key}><td>{run.stage}</td><td><Badge tone={run.status === "completed" ? "good" : run.status === "partial" ? "warn" : "bad"}>{run.status}</Badge></td><td className="nowrap">{time(run.started_at)}</td><td className="nowrap">{time(run.finished_at)}</td><td>{sum.pending?.created ?? "-"}</td><td>{sum.done?.new_items ?? "-"}</td><td>{sum.done?.markdown_tasks_enqueued ?? "-"}</td></tr>
      })}
      {!data.recent_runs.length && <tr><td colSpan={7} className="empty">尚无定时运行记录</td></tr>}
    </tbody></table></div>

    {confirming && <div className="drawer-layer" role="dialog" aria-modal="true"><button className="drawer-scrim" aria-label="取消" onClick={() => setConfirming(null)}/><aside className="drawer confirm-drawer"><header><div><small>二次确认</small><h2>{confirming.label}</h2></div><button className="icon-button" onClick={() => setConfirming(null)}><X size={19}/></button></header><div className="drawer-body"><p>该操作会改动本机 systemd 服务状态，确定要继续吗？</p><div className="toolbar-actions"><button className="button-danger" disabled={acting !== ""} onClick={() => { const c = confirming; setConfirming(null); void dispatch(c) }}>{acting || "确认执行"}</button><button onClick={() => setConfirming(null)}>取消</button></div></div></aside></div>}
  </section>
}
function Field({label,value,change}:{label:string;value:string;change:(v:string)=>void}){return <label className="setting-field"><span>{label}</span><input value={value} onChange={e=>change(e.target.value)}/></label>}
function NumberField({label,value,change,step="1"}:{label:string;value:number;change:(v:number)=>void;step?:string}){return <label className="setting-field"><span>{label}</span><input type="number" step={step} value={value} onChange={e=>change(Number(e.target.value))}/></label>}
function Toggle({label,checked,change}:{label:string;checked:boolean;change:(v:boolean)=>void}){return <label className="setting-toggle"><span>{label}</span><input type="checkbox" checked={checked} onChange={e=>change(e.target.checked)}/></label>}
function SecretState({label,configured}:{label:string;configured:boolean}){return <div className="secret-state"><span>{label}</span><Badge tone={configured?"good":"warn"}>{configured?"已配置":"未配置"}</Badge></div>}
function Metric({ label, value, bad }: { label: string; value: number | string; bad?: boolean }) { return <div className={`metric ${bad ? "metric-bad" : ""}`}><span>{label}</span><strong>{typeof value === "number" ? value.toLocaleString() : value}</strong></div> }
function Service({ icon: Icon, title, status, detail }: { icon: typeof Server; title: string; status: string; detail: string }) { const healthy = ["running", "healthy", "ok", "idle"].includes(status); return <div className="service"><Icon size={21}/><div><strong>{title}</strong><small>{detail}</small></div><Badge tone={healthy ? "good" : "warn"}>{status}</Badge></div> }
const labelFor = (key: string) => ({ pending: "当前待办", done: "已办事项", files: "文件记录", snapshots: "事项采集记录", source_attachments: "来源附件", archive_packages: "压缩包", archive_members: "压缩包成员", parse_artifacts: "解析产物", knowledge_documents: "知识文档", failed_or_retry: "失败或待重试" }[key] || key)

function Drawer({ title, subtitle, close, children }: { title: string; subtitle: string; close: () => void; children: React.ReactNode }) { return <div className="drawer-layer" role="dialog" aria-modal="true"><button className="drawer-scrim" aria-label="关闭详情" onClick={close}/><aside className="drawer"><header><div><small>{subtitle}</small><h2>{title}</h2></div><button className="icon-button" title="关闭" onClick={close}><X size={19}/></button></header><div className="drawer-body">{children}</div></aside></div> }
function PendingDrawer({ data, close }: { data: PendingDetail; close: () => void }) { return <Drawer title={data.title} subtitle={`逻辑事项 #${data.logical_item_id}`} close={close}><div className="detail-grid"><Info label="发起人" value={data.sender}/><Info label="当前节点" value={data.current_node}/><Info label="采集记录" value={data.snapshot ? `${captureLabel(data.snapshot.kind)} · 第${data.snapshot.version}版` : "尚未采集"}/><Info label="生命周期关联" value="事项ID稳定关联"/></div><h3>身份与证据</h3><div className="evidence-list">{Object.entries(data.identity).map(([key, ok]) => <div key={key}><span>{key}</span><Badge tone={ok ? "good" : "bad"}>{ok ? "已采集" : "缺失"}</Badge></div>)}{data.evidence_files.map(file => <div key={file.id}><span>{file.role}<small>{file.local_relpath}</small></span><Badge tone={file.status === "verified" ? "good" : "warn"}>{file.status}</Badge></div>)}</div><h3>附件清单</h3>{data.attachments.length ? <div className="attachment-list">{data.attachments.map(a => <div className="attachment" key={a.id}><FileText size={20}/><span><strong>{a.name}</strong><small>{size(a.size_bytes)} · {a.local_relpath || "尚无本地路径"}</small></span><Badge tone={a.status === "verified" ? "good" : "bad"}>{a.status}</Badge>{a.content_reused && <Badge tone="info">内容复用</Badge>}{a.error_code && <p>{a.error_code}</p>}{a.archive && <div className="archive-tree"><strong>{a.archive.format} · {a.archive.security_status}</strong>{a.archive.members.map(m => <span key={m.id}>{m.path} · {m.status}</span>)}</div>}</div>)}</div> : <div className="empty panel">当前采集记录尚未同步附件清单。此状态不代表“确认无附件”。</div>}</Drawer> }
function KnowledgeDrawer({ data, close }: { data: KnowledgeDetail; close: () => void }) { return <Drawer title={data.title} subtitle={`知识文档 #${data.id}`} close={close}><div className="detail-grid"><Info label="发布状态" value={data.publish_status}/><Info label="Vault 路径" value={data.vault_relpath}/><Info label="解析器" value={data.artifact?.engine || "-"}/><Info label="质量分" value={data.artifact?.quality_score?.toFixed(2) || "-"}/></div><h3>来源关系</h3><div className="evidence-list">{data.sources.map((source, index) => <div key={index}><span>OA #{source.oa_item_id ?? "-"}</span><span>文件 #{source.source_file_id ?? "-"}</span></div>)}</div><h3>Markdown 预览</h3><pre className="markdown-preview">{data.preview || "暂无预览"}</pre></Drawer> }
function Info({ label, value }: { label: string; value: string | null | undefined }) { return <div className="info"><span>{label}</span><strong>{value || "-"}</strong></div> }

export default App
