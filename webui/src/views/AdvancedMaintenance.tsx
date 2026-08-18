import { useCallback, useEffect, useRef, useState } from "react"
import {
  Bell, ChevronRight, CircleAlert, Database, Gauge, HardDrive, ListChecks, RefreshCw,
  Server, ShieldCheck, Trash2, X, RotateCcw,
} from "lucide-react"
import type {
  PendingItem, PendingDetail, ProcessingData, OnlineAuditData, MarkdownDoc,
  SourceReview, GovernanceRun, GovernanceStorage, IntegrityAudit, ArchiveMigration,
  ScheduleData, MaintenanceData, ServiceStatus, JobStatus,
} from "../App"
import {
  api, postApi, csrf, time, size, Badge, Metric, Info, Progress, SearchBox, ServiceCard, SERVICE_TITLES,
} from "../App"

// ---------------------------------------------------------------------------
// 状态中文化（与既有 WebUI 一致，仅高级维护使用）
// ---------------------------------------------------------------------------

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
const captureLabel = (value: string | null) => value === "pending_initial" ? "首次采集" : value === "pending_updated" ? "更新采集" : value || "已采集"

const DONE_STAGE_LABELS = [
  ["discovery", "发现"], ["download", "下载"], ["verification", "校验"],
  ["markdown", "MD 化"], ["curation", "编目"], ["publication", "发布"],
] as const
const DONE_STAGE_STATE_LABELS: Record<string, string> = {
  pending: "等待", running: "处理中", done: "已完成", failed: "失败", review: "待复核",
}
function DoneStageStrip({ stages }: { stages: PendingDetail["stages"] | Record<"discovery" | "download" | "verification" | "markdown" | "curation" | "publication", "pending" | "running" | "done" | "failed" | "review"> }) {
  return <div className="done-stage-strip" role="list" aria-label="流水线阶段">
    {DONE_STAGE_LABELS.map(([key, label]) => <span key={key} role="listitem" className={`done-stage done-stage-${stages[key as keyof typeof stages]}`} title={`${label}：${DONE_STAGE_STATE_LABELS[stages[key as keyof typeof stages]]}`}>{label}</span>)}
  </div>
}

const AUDIT_LABELS: Record<string, string> = {
  queued: "等待开始", running: "正在逐项核验", pause_requested: "正在安全暂停", paused: "已暂停",
  completed: "核验完成", failed: "核验失败", superseded: "已由新核验替代",
  pending: "待核验", matched: "完全一致", missing_download: "本地缺少原件",
  historical_retained: "本地有历史保留文件", inventory_mismatch: "附件清单不一致",
  content_mismatch: "原件内容不一致", content_unverified: "线上内容未完整读取",
  depth_limit_reached: "达到十层深度上限", access_failed: "OA 访问失败",
}
const AUDIT_REASON_LABELS: Record<string, string> = {
  exact_match: "附件身份与内容均一致",
  attachment_identity_changed: "附件标识发生变化，角色、大小与内容一致",
  attachment_role_changed: "附件角色发生变化，标识、大小与内容一致",
  attachment_metadata_changed: "附件标识和角色发生变化，但大小与内容一致",
  content_changed: "同一附件标识的大小或内容发生变化",
  inventory_changed: "附件集合存在新增、缺失或多项变化",
  historical_retained: "线上当前文件均已保留，本地另有历史版本",
  evidence_unavailable: "旧核验记录缺少逐附件证据",
}
const SOURCE_REVIEW_LABELS: Record<string, string> = {
  UNSUPPORTED_SOURCE_FORMAT: "暂不支持的文件格式",
  PARSE_QUALITY_REJECTED: "解析质量未达到发布标准",
  UNSAFE_SOURCE_PATH: "源文件路径不符合安全规则",
}
const GOVERNANCE_STATUS: Record<string, string> = {
  planning: "正在预检", planned: "等待隔离", quarantining: "正在隔离", quarantined: "已隔离",
  restoring: "正在恢复", restored: "已恢复", purging: "正在清除", purged: "已清除", failed: "执行失败",
}
const GOVERNANCE_CATEGORY: Record<string, string> = {
  browser_cache: "浏览器缓存", runtime_reports: "运行报告", expired_backups: "过期备份",
  sent_pending_orphans: "已发送待办残留", rebuildable_projection: "可重建派生资料",
  unreferenced_legacy: "待核验历史资料",
}

// ---------------------------------------------------------------------------
// 高级维护子视图（与既有能力一致，仅在此折叠区渲染）
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
  const [detail, setDetail] = useState<PendingDetail>(data)
  const [syncing, setSyncing] = useState(false)
  const cleaned = detail.cleanup_status === "cleaned" || detail.occurrence_status === "cleaned"
  const oaGone = detail.oa_gone_at != null
  const run = async (label: string, path: string, body?: unknown) => {
    try { await postApi(path, body); alert(`${label}已触发，请稍后在列表中查看结果`) }
    catch (reason) { alert(`${label}失败：${reason instanceof Error ? reason.message : "未知错误"}`) }
  }
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
      } catch (reason) { console.error("与 OA 同步失败", reason) }
      finally { if (!cancelled) setSyncing(false) }
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
      <DoneStageStrip stages={detail.stages}/>
      {detail.requires_delivery_reconciliation && <div className="settings-message" role="alert">飞书返回结果不确定，消息可能已经送达。系统已禁止自动重发和清理，请先在飞书侧人工确认。</div>}
      <div className="drawer-actions">
        <button disabled={syncing} onClick={() => { setDetail(data); void (async () => {
          setSyncing(true)
          try { await postApi(`/api/pending-notifications/${detail.id}/sync-oa`); setDetail(await api<PendingDetail>(`/api/pending-notifications/${detail.id}`)) }
          catch (reason) { alert(`与 OA 同步失败：${reason instanceof Error ? reason.message : "未知错误"}`) }
          finally { setSyncing(false) }
        })() }}><RefreshCw size={16}/>与 OA 同步</button>
        <button onClick={() => void run("重试摘要", `/api/pending-notifications/${detail.id}/retry-summary`)}><RotateCcw size={16}/>重试摘要</button>
        {detail.can_retry_delivery && <button onClick={() => void run("重试发送", `/api/pending-notifications/${detail.id}/retry-delivery`)}><Bell size={16}/>重试发送</button>}
        {detail.can_cleanup && <button className="button-danger" onClick={() => { if (confirm("清理已确认发送成功的待办业务数据？将仅保留最小去重台账。")) void run("清理", `/api/pending-notifications/${detail.id}/cleanup`) }}><Trash2 size={16}/>清理已发送数据</button>}
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

function OnlineVerificationView({ data, reload }: { data: OnlineAuditData | null; reload: () => Promise<void> }) {
  const [message, setMessage] = useState("")
  const run = data?.run
  const reviewCount = Math.max(0, (run?.mismatch_items || 0) - (run?.missing_download_items || 0) - (run?.local_extra_items || 0))
  const active = !!run && ["queued", "running", "pause_requested", "paused"].includes(run.status)
  const action = async (name: "start" | "pause" | "resume") => {
    if (name === "start" && !confirm("开始后会逐项读取 OA 已办附件并计算内容指纹，耗时较长，但不会覆盖本地原件。继续吗？")) return
    try {
      const path = name === "start" ? "/api/audits/online" : `/api/audits/online/${run?.id}/${name}`
      await postApi(path)
      setMessage(name === "start" ? "逐项线上核验已进入队列" : name === "pause" ? "已请求安全暂停" : "已继续核验")
      await reload()
    } catch (reason) { setMessage(`操作失败：${reason instanceof Error ? reason.message : "未知错误"}`) }
  }
  return <section>
    <div className="governance-hero">
      <div><span className="eyebrow">OA 只读核验</span><h2>线上逐项一致性核验</h2><p>建议先在系统设置中执行“立即扫描已办”，同步完整线上清单。核验会重新读取线上附件，在内存中比对附件身份、大小和 SHA256；不覆盖本地原件。</p></div>
      <div className="toolbar-actions">
        {!active && <button className="button-primary" onClick={() => void action("start")}><ShieldCheck size={16}/>启动逐项核验</button>}
        {run?.status === "paused" && <button onClick={() => void action("resume")}><RefreshCw size={16}/>继续核验</button>}
        {run && ["queued", "running"].includes(run.status) && <button onClick={() => void action("pause")}><CircleAlert size={16}/>安全暂停</button>}
      </div>
    </div>
    {message && <div className="settings-message" role="status">{message}</div>}
    <div className="metrics">
      <Metric label="核验状态" value={run ? (AUDIT_LABELS[run.status] || run.status) : "尚未运行"}/>
      <Metric label="总事项" value={run?.total_items || 0}/><Metric label="已核验" value={run?.completed_items || 0}/>
      <Metric label="完全一致" value={run?.matched_items || 0}/><Metric label="待补下载" value={run?.missing_download_items || 0} bad={(run?.missing_download_items || 0) > 0}/>
      <Metric label="本地历史保留" value={run?.local_extra_items || 0}/><Metric label="待人工复核" value={reviewCount} bad={reviewCount > 0}/>
      <Metric label="访问失败" value={run?.access_failed_items || 0}/>
    </div>
    {run && run.total_items > 0 && <Progress label="线上逐项核验进度" value={Math.round(run.completed_items * 100 / run.total_items)} />}
    {data && Object.keys(data.comparison_reasons).length > 0 && <div className="detail-grid">
      {Object.entries(data.comparison_reasons).map(([reason, count]) => <Info key={reason} label={AUDIT_REASON_LABELS[reason] || reason} value={String(count)}/>)}
    </div>}
    {data?.items.some(item => !["pending", "matched"].includes(item.status)) && <details className="advanced"><summary>查看本页差异与历史保留</summary><ul className="diag-list">
      {data.items.filter(item => !["pending", "matched"].includes(item.status)).map(item => <li key={item.id}>{item.title}：{AUDIT_LABELS[item.status] || item.status}（线上 {item.recognized_attachments ?? "-"} / 本地 {item.downloaded_attachments}；{AUDIT_REASON_LABELS[item.comparison_reason || ""] || "等待差异解释"}）</li>)}
    </ul></details>}
  </section>
}

function SourceReviewQueue({ rows, reload }: { rows: SourceReview[]; reload: () => Promise<void> }) {
  const [message, setMessage] = useState("")
  const handle = async (id: number, action: "retry" | "dismissed") => {
    try {
      if (action === "retry") await postApi(`/api/reviews/${id}/retry-source`)
      else await postApi(`/api/reviews/${id}/resolve`, { resolution: "dismissed" })
      setMessage(action === "retry" ? "已重新排入知识整理流水线" : "已从待复核列表忽略")
      await reload()
    } catch (reason) { setMessage(`操作失败：${reason instanceof Error ? reason.message : "未知错误"}`) }
  }
  const counts = rows.reduce<Record<string, number>>((acc, row) => {
    const reason = row.details.reason_code || "UNKNOWN"
    acc[reason] = (acc[reason] || 0) + 1
    return acc
  }, {})
  return <section>
    <div className="section-toolbar"><div><h2>Source Markdown 人工复核</h2><p>不支持格式、质量不足或路径异常会在这里停住，不会反复重试，也不会发布半成品。处理原文件或解析能力后，请在下方已办事项中重试对应步骤。</p></div></div>
    <div className="metrics">
      <Metric label="待人工复核" value={rows.length} bad={rows.length > 0}/>
      {Object.entries(counts).slice(0, 2).map(([reason, count]) => <Metric key={reason} label={SOURCE_REVIEW_LABELS[reason] || "其他原因"} value={count}/>) }
    </div>
    {message && <div className="settings-message" role="status">{message}</div>}
    {rows.length > 0 && <details className="advanced"><summary>查看待复核记录</summary>
      <div className="table-wrap"><table><thead><tr><th>记录</th><th>原因</th><th>发现时间</th><th>处理</th></tr></thead><tbody>
        {rows.map(row => <tr key={row.id}><td>复核记录 #{row.id}</td><td><Badge tone="warn">{SOURCE_REVIEW_LABELS[row.details.reason_code || ""] || "需要人工判断"}</Badge></td><td>{time(row.created_at)}</td><td><div className="toolbar-actions"><button onClick={() => void handle(row.id, "retry")}>重新检测并继续整理</button><button onClick={() => void handle(row.id, "dismissed")}>忽略</button></div></td></tr>)}
      </tbody></table></div>
    </details>}
  </section>
}

function MarkdownOutputsView({ rows, total, page, setPage, reload }: { rows: MarkdownDoc[]; total: number; page: number; setPage: (page: number) => void; reload: () => Promise<void> }) {
  const pages = Math.max(1, Math.ceil(total / 50))
  const runAction = async (label: string, path: string) => {
    try { await postApi(path); await reload() }
    catch (reason) { alert(`${label}失败：${reason instanceof Error ? reason.message : "未知错误"}`) }
  }
  return <section>
    <div className="metrics"><Metric label="Markdown 总数" value={total}/><Metric label="本页已交付" value={rows.filter(r => r.delivery_status === "exported").length}/><Metric label="本页待交付" value={rows.filter(r => r.delivery_status !== "exported").length}/></div>
    <div className="section-toolbar"><div><h2>Source Markdown</h2><p>忠实保留正文及附件内容的唯一 Markdown 来源，可由原始归档重新生成，并作为本地 qwen3.5:9b 去噪归类的输入。</p></div></div>
    <div className="table-wrap"><table style={{ minWidth: 1100 }}><thead><tr><th className="title-col">Markdown 文件</th><th>原始附件</th><th>来源事项</th><th>解析引擎</th><th>质量</th><th>输出时间</th><th>OARadar 路径</th><th>交付状态</th><th aria-label="操作"/></tr></thead><tbody>
      {rows.map(row => <tr key={row.id}>
        <td className="title-cell"><strong>{row.markdown_relpath}</strong><small>发布路径：{row.llm_wiki_path}</small></td>
        <td>{row.source_file || "-"}</td>
        <td>{row.source_oa_item || "-"}</td>
        <td>{row.engine}</td>
        <td><Badge tone={row.quality === "passed" ? "good" : row.quality === "failed" ? "bad" : "warn"}>{row.quality}</Badge></td>
        <td className="nowrap">{time(row.generated_at)}</td>
        <td className="path-cell" title={row.oaradar_path}>{row.oaradar_path}</td>
        <td><Badge tone={row.delivery_status === "exported" ? "good" : "bad"}>{row.delivery_status === "exported" ? "已交付" : "待交付"}</Badge></td>
        <td><button className="row-action" title="重新生成" onClick={() => void runAction("重新生成", `/api/markdown-outputs/${row.id}/rebuild`)}><RotateCcw size={15}/></button></td>
      </tr>)}
      {!rows.length && <tr><td colSpan={9} className="empty">尚无 Source Markdown</td></tr>}
    </tbody></table></div>
    <div className="pagination"><button disabled={page <= 1} onClick={() => setPage(page - 1)}>上一页</button><span>第 {page}/{pages} 页</span><button disabled={page >= pages} onClick={() => setPage(page + 1)}>下一页</button></div>
  </section>
}

function ProcessingCenter({ data, reload }: { data: ProcessingData | null; reload: () => Promise<void> }) {
  const [message, setMessage] = useState("")
  const history = data?.queues.historical_done_backfill
  const controlCampaign = async (action: "start" | "pause" | "resume") => {
    if (action === "start" && !confirm("为所有已下载的存量已办建立知识整理任务？原始文件不会被修改。")) return
    try {
      const result = await postApi<{ created?: number; requeued?: number; repaired_legacy?: number; already_active?: number }>(
        action === "start" ? "/api/knowledge/rebuild" : `/api/knowledge/rebuild/${action}`,
      )
      setMessage(action === "start"
        ? result.already_active
          ? `整理任务已在运行（当前 ${result.already_active} 项），补入 ${result.created || 0} 项，修复旧状态 ${result.repaired_legacy || 0} 项`
          : `已新增 ${result.created || 0} 项、重新排队 ${result.requeued || 0} 项`
        : `${action === "pause" ? "暂停整理" : "继续整理"}成功`)
      await reload()
    } catch (reason) { setMessage(`操作失败：${reason instanceof Error ? reason.message : "未知错误"}`) }
  }
  return <section>
    <div className="section-toolbar"><div><h2>处理中心 / 任务队列</h2><p>全量整理按“原件核验 → 唯一源 Markdown → 本地模型归类 → 发布知识目录”执行，不修改原始已办。</p></div>
      <div className="toolbar-actions">
        {data?.historical_paused ? <button onClick={() => void controlCampaign("resume")}><RefreshCw size={16}/>继续整理</button>
          : (history?.queued || history?.running) ? <button onClick={() => void controlCampaign("pause")}><CircleAlert size={16}/>暂停整理</button>
          : <button className="button-primary" onClick={() => void controlCampaign("start")}><ListChecks size={16}/>启动全量整理</button>}
      </div></div>
    <div className="metrics">
      <Metric label="整理待处理" value={(history?.queued || 0) + (history?.running || 0)}/>
      <Metric label="整理失败" value={history?.failed || 0} bad={(history?.failed || 0) > 0}/>
      <Metric label="已暂停" value={data?.historical_paused ? "是" : "否"}/>
    </div>
    {message && <div className="settings-message" role="status">{message}</div>}
  </section>
}

function DataGovernanceView({ rows, integrity, migration, storage, reload }: { rows: GovernanceRun[]; integrity: IntegrityAudit | null; migration: ArchiveMigration | null; storage: GovernanceStorage | null; reload: () => Promise<void> }) {
  const [acting, setActing] = useState("")
  const [message, setMessage] = useState("")
  const queue = async (label: string, path: string, body: unknown) => {
    setActing(label); setMessage("")
    try { await postApi(path, body); setMessage(`${label}任务已进入队列`); await reload() }
    catch (reason) { setMessage(`${label}失败：${reason instanceof Error ? reason.message : "未知错误"}`) }
    finally { setActing("") }
  }
  const plan = (includeDerived: boolean) => {
    const categories = ["browser_cache", "runtime_reports", "expired_backups", "sent_pending_orphans"]
    if (includeDerived) categories.push("rebuildable_projection")
    void queue("生成清理预检", "/api/data-governance/plans", { categories })
  }
  const action = (run: GovernanceRun, name: "quarantine" | "restore" | "purge") => {
    if (name === "quarantine" && !confirm(`隔离第 ${run.id} 号计划中的 ${run.candidate_count} 个候选？原始已办不会被纳入。`)) return
    if (name === "restore" && !confirm(`恢复第 ${run.id} 号隔离计划？已重建的同名文件不会被覆盖。`)) return
    let confirmation: string | undefined
    if (name === "purge") {
      const expected = `PURGE-CLEANUP-RUN-${run.id}`
      confirmation = prompt(`永久清除不可恢复。请输入：${expected}`) || undefined
      if (confirmation !== expected) { setMessage("确认串不匹配，未创建清除任务"); return }
    }
    void queue(name === "quarantine" ? "隔离" : name === "restore" ? "恢复" : "永久清除", `/api/data-governance/runs/${run.id}/${name}`, { confirmation })
  }
  return <section>
    <div className="governance-hero">
      <div><span className="eyebrow">本地数据治理</span><h2>先预检，再隔离，最后验证</h2><p>系统仅显示数量与空间汇总。原始已办、活动数据库、登录态、活动任务和待复核异常永不进入候选。</p></div>
      <div className="toolbar-actions">
        <button disabled={!!acting} onClick={() => plan(false)}><ShieldCheck size={16}/>安全预检</button>
        <button disabled={!!acting} onClick={() => { if (confirm("可重建的 parse、vault、workspace 也会进入隔离候选，是否继续？")) plan(true) }}><HardDrive size={16}/>预检全部派生资料</button>
      </div>
    </div>
    <div className="governance-storage-metrics">
      <Metric label="受保护原始已办" value={storage ? `${storage.originals.items} 项` : "-"}/>
      <Metric label="已核验原件" value={storage ? `${storage.originals.files} 个 · ${size(storage.originals.bytes)}` : "-"}/>
      <Metric label="数据库" value={storage ? size(storage.database_bytes) : "-"}/>
      <Metric label="磁盘可用" value={storage ? size(storage.disk_free_bytes) : "-"}/>
      <Metric label="活动任务" value={storage?.active_tasks ?? "-"}/>
      <Metric label="待人工复核" value={storage?.pending_reviews ?? "-"} bad={(storage?.pending_reviews || 0) > 0}/>
      <Metric label="隔离区（可恢复）" value={storage ? `${storage.quarantine.count} 个 · ${size(storage.quarantine.bytes)}` : "-"}/>
    </div>
    <div className="governance-tier-grid">
      {(storage?.tiers || []).map(tier => <article className={`governance-tier ${tier.protected ? "governance-tier-protected" : ""}`} key={tier.id}>
        <header><strong>{tier.label}</strong><Badge tone={tier.protected ? "good" : "neutral"}>{tier.protected ? "受保护" : "可治理"}</Badge></header>
        <p>{tier.retention}</p>
        <dl><div><dt>对象</dt><dd>{tier.count}</dd></div><div><dt>容量</dt><dd>{size(tier.bytes)}</dd></div><div><dt>数据库引用</dt><dd>{tier.database_references}</dd></div></dl>
      </article>)}
      {!storage?.tiers.length && <div className="empty governance-tier-empty">容量汇总加载中</div>}
    </div>
    <div className="governance-category-grid">
      {["browser_cache", "runtime_reports", "expired_backups", "sent_pending_orphans", "rebuildable_projection"].map(category => {
        const summary = storage?.category_summary[category] || { count: 0, bytes: 0 }
        return <div key={category}><span>{GOVERNANCE_CATEGORY[category]}</span><strong>{summary.count}</strong><small>{size(summary.bytes)}</small></div>
      })}
    </div>
    <div className="metrics compact-metrics">
      <Metric label="迁移状态" value={migration ? ({ queued: "等待中", running: "迁移中", completed: "已完成", failed: "失败" }[migration.status] || migration.status) : "等待线上核验"}/>
      <Metric label="迁移进度" value={migration ? `${migration.progress_current}/${migration.progress_total ?? "-"}` : "-"}/>
      <Metric label="已安全迁移" value={migration?.migrated ?? 0}/>
      <Metric label="迁移失败" value={migration?.failed ?? 0} bad={(migration?.failed || 0) > 0}/>
      <Metric label="保持原位待复核" value={migration?.review_required ?? 0} bad={(migration?.review_required || 0) > 0}/>
    </div>
    <div className="metrics compact-metrics">
      <Metric label="内容变化（冻结）" value={integrity?.reason_counts.content_changed || 0} bad={(integrity?.reason_counts.content_changed || 0) > 0}/>
      <Metric label="历史清单格式漂移" value={integrity?.reason_counts.manifest_schema_drift || 0}/>
      <Metric label="需人工复核" value={integrity?.reason_counts.review_required || 0} bad={(integrity?.reason_counts.review_required || 0) > 0}/>
      <Metric label="真实缺失原件" value={integrity?.reason_counts.real_missing_source || 0} bad={(integrity?.reason_counts.real_missing_source || 0) > 0}/>
      <Metric label="异常合计" value={integrity?.total || 0}/>
    </div>
    {message && <div className="settings-message" role="status">{message}</div>}
    <div className="table-wrap"><table style={{ minWidth: 980 }}><thead><tr><th>计划</th><th>状态</th><th className="title-col">范围</th><th>候选</th><th>预计空间</th><th>已隔离</th><th>已恢复</th><th>已清除</th><th aria-label="操作"/></tr></thead><tbody>
      {rows.map(run => <tr key={run.id}>
        <td>#{run.id}</td>
        <td><Badge tone={run.status === "failed" ? "bad" : run.status === "purged" || run.status === "restored" ? "good" : "warn"}>{GOVERNANCE_STATUS[run.status] || run.status}</Badge></td>
        <td>{run.categories.map(category => GOVERNANCE_CATEGORY[category] || category).join("、") || "-"}</td>
        <td>{run.candidate_count}</td><td>{size(run.candidate_bytes)}</td>
        <td>{run.quarantined_count}</td><td>{run.restored_count}</td><td>{run.purged_count}</td>
        <td><div className="row-actions">
          {run.status === "planned" && <button disabled={!!acting} onClick={() => action(run, "quarantine")}>隔离</button>}
          {run.status === "quarantined" && <><button disabled={!!acting} onClick={() => action(run, "restore")}>恢复</button><button className="danger-link" disabled={!!acting} onClick={() => action(run, "purge")}>清除</button></>}
        </div></td>
      </tr>)}
      {!rows.length && <tr><td colSpan={9} className="empty">尚未生成清理预检。建议先运行“安全预检”。</td></tr>}
    </tbody></table></div>
  </section>
}

function MaintenanceActions({ schedule, diag, reload }: { schedule: ScheduleData | null; diag: MaintenanceData | null; reload: () => Promise<void> }) {
  const [acting, setActing] = useState("")
  const [message, setMessage] = useState("")
  const action = async (label: string, path: string, body?: unknown) => {
    setActing(label)
    try { await postApi(path, body); setMessage(`${label}已触发`); setScheduleViaReload() }
    catch (reason) { setMessage(`${label}失败：${reason instanceof Error ? reason.message : "未知错误"}`) }
    finally { setActing("") }
  }
  // 复用父级 reload 以刷新 schedule/diag
  const setScheduleViaReload = () => { void reload() }
  return <section className="settings-panel">
    <h2><Server size={18}/>运行维护</h2>
    <p className="settings-note">仅用于异常处理。危险操作会改动本机服务状态。</p>
    {schedule && <div className="service-grid-5">{Object.entries(schedule.services).map(([key, svc]) => <ServiceCard key={key} title={SERVICE_TITLES[key] || key} svc={svc}/>)}</div>}
    {schedule && <>
      <div className="section-toolbar"><div><h3>最近已办全量扫描</h3><p>最近完成：{time(schedule.summary.nightly.last_at)}。基线补齐只建立版本指纹，不会触发重复下载。</p></div></div>
      <div className="data-grid">
        <div className="data-cell"><span>线上事项</span><strong>{schedule.summary.nightly.source_total.toLocaleString()}</strong></div>
        <div className="data-cell"><span>扫描页数</span><strong>{schedule.summary.nightly.pages_scanned.toLocaleString()}</strong></div>
        <div className="data-cell"><span>基线补齐</span><strong>{schedule.summary.nightly.baseline_hashes.toLocaleString()}</strong></div>
        <div className="data-cell"><span>新增</span><strong>{schedule.summary.nightly.new_items.toLocaleString()}</strong></div>
        <div className={`data-cell ${schedule.summary.nightly.changed_items ? "data-cell-bad" : ""}`}><span>内容变化</span><strong>{schedule.summary.nightly.changed_items.toLocaleString()}</strong></div>
        <div className={`data-cell ${schedule.summary.nightly.retry_items ? "data-cell-bad" : ""}`}><span>重试项</span><strong>{schedule.summary.nightly.retry_items.toLocaleString()}</strong></div>
        <div className="data-cell"><span>下载入队</span><strong>{schedule.summary.nightly.download_jobs_enqueued.toLocaleString()}</strong></div>
        <div className="data-cell"><span>知识任务入队</span><strong>{schedule.summary.nightly.knowledge_tasks_enqueued.toLocaleString()}</strong></div>
      </div>
    </>}
    <div className="toolbar-actions">
      <button disabled={!!acting} onClick={() => void action("立即扫描待办", "/api/schedule/hourly")}><RefreshCw size={16}/>立即扫描待办</button>
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
    {message && <div className="settings-message">{message}</div>}
  </section>
}

// ---------------------------------------------------------------------------
// 高级维护折叠容器：默认不请求重接口，首次展开才加载并轮询
// ---------------------------------------------------------------------------

interface MaintenanceDataState {
  onlineAudit: OnlineAuditData | null
  sourceReviews: SourceReview[]
  markdown: MarkdownDoc[]
  markdownTotal: number
  markdownPage: number
  processing: ProcessingData | null
  governance: GovernanceRun[]
  integrity: IntegrityAudit | null
  migration: ArchiveMigration | null
  storage: GovernanceStorage | null
  pending: PendingItem[]
  pendingTotal: number
  pendingFilter: string
  schedule: ScheduleData | null
  diag: MaintenanceData | null
}

const EMPTY: MaintenanceDataState = {
  onlineAudit: null, sourceReviews: [], markdown: [], markdownTotal: 0, markdownPage: 1,
  processing: null, governance: [], integrity: null, migration: null, storage: null,
  pending: [], pendingTotal: 0, pendingFilter: "", schedule: null, diag: null,
}

export function AdvancedMaintenance({ open, onToggle }: { open: boolean; onToggle: (value: boolean) => void }) {
  const [data, setData] = useState<MaintenanceDataState>(EMPTY)
  const [pendingDetail, setPendingDetail] = useState<PendingDetail | null>(null)
  const [pendingQuery, setPendingQuery] = useState("")
  const loadedRef = useRef(false)
  const openRef = useRef(open)
  openRef.current = open

  const reload = useCallback(async (silent = false) => {
    try {
      const [onlineAudit, sourceReviews, md, processing, gov, pending, schedule, diag] = await Promise.all([
        api<OnlineAuditData>("/api/audits/online?item_page=1&item_page_size=50"),
        api<SourceReview[]>("/api/reviews?status=pending&kind=source_markdown_incomplete"),
        api<{ documents: MarkdownDoc[]; total: number; page: number; page_size: number }>(`/api/markdown-outputs?page=${data.markdownPage}&page_size=50`),
        api<ProcessingData>("/api/lifecycle/processing-center"),
        api<{ runs: GovernanceRun[]; integrity: IntegrityAudit | null; archive_migration: ArchiveMigration | null; storage: GovernanceStorage }>("/api/data-governance"),
        api<{ items: PendingItem[]; total: number }>(`/api/pending-notifications${data.pendingFilter ? `?filter=${data.pendingFilter}` : ""}`),
        api<ScheduleData>("/api/schedule/status"),
        api<MaintenanceData>("/api/maintenance"),
      ])
      setData(d => ({
        ...d, onlineAudit, sourceReviews, markdown: md.documents, markdownTotal: md.total,
        processing, governance: gov.runs, integrity: gov.integrity, migration: gov.archive_migration,
        storage: gov.storage, pending: pending.items, pendingTotal: pending.total, schedule, diag,
      }))
    } catch (reason) {
      if (!silent) console.error("高级维护加载失败", reason)
    }
  }, [data.markdownPage, data.pendingFilter])

  const openPending = async (id: number) => {
    try { setPendingDetail(await api<PendingDetail>(`/api/pending-notifications/${id}`)) }
    catch (reason) { alert(reason instanceof Error ? reason.message : "详情加载失败") }
  }

  useEffect(() => {
    if (!open) return
    if (!loadedRef.current) { loadedRef.current = true; void reload() }
    const timer = window.setInterval(() => void reload(true), 5000)
    return () => window.clearInterval(timer)
  }, [open, reload])

  if (!open) {
    return <section className="settings-panel advanced-maintenance">
      <div className="section-toolbar"><div><h2><Gauge size={18}/>高级维护</h2><p>在线逐项核验、Source Markdown 明细、人工复核、数据治理与运行维护。默认折叠，展开才加载。</p></div></div>
      <button className="button-primary" aria-expanded={open} onClick={() => onToggle(true)}>展开高级维护</button>
    </section>
  }

  return <section className="settings-stack advanced-maintenance">
    <div className="settings-panel">
      <div className="section-toolbar"><div><h2><Gauge size={18}/>高级维护</h2><p>以下均为只读诊断与维护操作，可安全折叠。</p></div>
        <div className="toolbar-actions"><button aria-expanded={open} onClick={() => onToggle(false)}>收起高级维护</button></div>
      </div>
    </div>
    <PendingNotificationsView
      rows={data.pending} total={data.pendingTotal} filter={data.pendingFilter}
      setFilter={f => setData(d => ({ ...d, pendingFilter: f }))}
      query={pendingQuery} setQuery={setPendingQuery} open={openPending} reload={() => reload()}
    />
    <OnlineVerificationView data={data.onlineAudit} reload={() => reload()}/>
    <SourceReviewQueue rows={data.sourceReviews} reload={() => reload()}/>
    <MarkdownOutputsView rows={data.markdown} total={data.markdownTotal} page={data.markdownPage} setPage={p => setData(d => ({ ...d, markdownPage: p }))} reload={() => reload()}/>
    <ProcessingCenter data={data.processing} reload={() => reload()}/>
    <DataGovernanceView rows={data.governance} integrity={data.integrity} migration={data.migration} storage={data.storage} reload={() => reload()}/>
    <MaintenanceActions schedule={data.schedule} diag={data.diag} reload={() => reload()}/>
    {pendingDetail && <PendingDrawer data={pendingDetail} close={() => setPendingDetail(null)}/>}
  </section>
}
