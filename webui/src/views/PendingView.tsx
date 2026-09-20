import { useCallback, useEffect, useRef, useState } from "react"
import { Search, X } from "lucide-react"
import { api, postApi, time } from "../App"
import type { PendingDetail } from "../App"

export type PendingRow = {
  id: number; title: string | null; sender: string | null; current_node: string | null
  received_at: string | null; last_seen_at?: string | null; summary_status: string; feishu_status: string; cleanup_status: string
  occurrence_status?: string; cleaned_at?: string | null
}

const FILTERS = [
  ["", "全部"], ["processing", "处理中"], ["summary_failed", "摘要失败"],
  ["feishu_failed", "飞书失败"], ["feishu_unknown", "投递待核对"],
  ["awaiting_cleanup", "待清理"], ["cleanup_failed", "清理失败"], ["recent_success", "已清理"],
]
const LABELS: Record<string, string> = {
  pending: "等待处理", queued: "已排队", running: "处理中", processing: "处理中", done: "已完成",
  current: "已生成", succeeded: "已完成", completed: "已完成", failed: "失败", sent: "已发送",
  rejected: "已拒绝", misconfigured: "配置异常", unknown: "投递待核对", unknown_outcome: "投递待核对",
  sending: "发送中", retrying: "等待重试", not_eligible: "未到清理条件", pending_cleanup: "待清理",
  cleaning: "清理中", cleaned: "已清理", cleanup_failed: "清理失败", disabled: "未启用",
  skipped: "已跳过", missing: "待生成", absent: "待生成", stale: "待更新", review: "需要核对",
}
const STAGES = [
  ["discovery", "发现事项"], ["download", "获取资料"], ["markdown", "正文转换"],
  ["summary", "生成摘要"], ["feishu", "飞书投递"], ["cleanup", "本地清理"],
] as const
function Status({ value }: { value: string }) {
  const tone = ["failed", "cleanup_failed", "rejected", "misconfigured"].includes(value) ? "bad"
    : ["unknown", "unknown_outcome", "review", "stale"].includes(value) ? "warn"
    : ["done", "sent", "cleaned", "current", "succeeded", "completed"].includes(value) ? "good" : "neutral"
  return <span className={`status status-${tone}`}>{LABELS[value] || "状态待确认"}</span>
}
function reasonText(reason: unknown) { return reason instanceof Error ? reason.message : "操作失败，请重试" }

function PendingDrawer({ id, close, refresh }: { id: number; close: () => void; refresh: () => Promise<void> }) {
  const [detail, setDetail] = useState<(PendingDetail & { can_retry_summary?: boolean; delivery_id?: number }) | null>(null)
  const [error, setError] = useState("")
  const [message, setMessage] = useState("")
  const [busy, setBusy] = useState(false)
  const dialogRef = useRef<HTMLElement>(null)
  const closeRef = useRef(close)
  closeRef.current = close
  const request = useRef(0)
  const mounted = useRef(true)
  const loadDetail = useCallback(async () => {
    const serial = ++request.current
    try {
      const result = await api<PendingDetail>(`/api/pending-notifications/${id}`)
      if (mounted.current && serial === request.current) { setDetail(result); setError("") }
    } catch (reason) {
      if (mounted.current && serial === request.current) setError(`详情更新失败：${reasonText(reason)}`)
    }
  }, [id])
  useEffect(() => {
    mounted.current = true
    void loadDetail()
    const timer = window.setInterval(() => void loadDetail(), 5000)
    return () => { mounted.current = false; request.current++; window.clearInterval(timer) }
  }, [loadDetail])
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = "hidden"
    dialogRef.current?.focus()
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") { event.preventDefault(); closeRef.current(); return }
      if (event.key !== "Tab") return
      const controls = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], input:not(:disabled), [tabindex="0"]') || [])
      const first = controls[0], last = controls[controls.length - 1]
      if (!first) { event.preventDefault(); return }
      if (event.shiftKey && (document.activeElement === first || document.activeElement === dialogRef.current)) {
        event.preventDefault(); last.focus()
      } else if (!event.shiftKey && (document.activeElement === last || document.activeElement === dialogRef.current)) {
        event.preventDefault(); first.focus()
      }
    }
    document.addEventListener("keydown", onKey)
    return () => { document.removeEventListener("keydown", onKey); document.body.style.overflow = previousOverflow; previous?.focus() }
  }, [])

  async function act(action: "retry-summary" | "retry-delivery" | "cleanup") {
    if (busy) return
    setBusy(true); setMessage(""); setError("")
    try {
      await postApi(`/api/pending-notifications/${id}/${action}`)
      if (!mounted.current) return
      // Drop any retained body immediately after a successful cleanup request.
      if (action === "cleanup") { request.current++; setDetail(null) }
      setMessage(action === "cleanup" ? "清理请求已提交，请查看最新清理状态。" : "重试请求已提交，处理进度会自动更新。")
      await loadDetail()
      try { await refresh() } catch (reason) { if (mounted.current) setError(`请求已提交，列表刷新失败：${reasonText(reason)}`) }
    } catch (reason) { if (mounted.current) setError(reasonText(reason)) }
    finally { if (mounted.current) setBusy(false) }
  }
  async function reconcileDelivery(outcome: "sent" | "failed") {
    if (!detail?.delivery_id || busy) return
    const observation = outcome === "sent" ? "已收到该通知" : "确认未收到该通知"
    if (!window.confirm(`请确认已经在飞书核对：${observation}。这只更新本地台账，不会自动发送或清理。`)) return
    setBusy(true); setMessage("")
    try {
      await postApi(`/api/pending-notifications/${id}/reconcile-delivery`, { delivery_id: detail.delivery_id, outcome, confirmed: true })
      setMessage("已记录核对结果；后续操作请查看更新后的状态。")
      await loadDetail(); await refresh()
    } catch (reason) { setMessage(reasonText(reason)) }
    finally { setBusy(false) }
  }
  const cleaned = detail?.cleanup_status === "cleaned" || detail?.occurrence_status === "cleaned"
  const reconcile = detail?.requires_delivery_reconciliation || ["unknown", "unknown_outcome"].includes(detail?.feishu_status || "")
  return <div className="drawer-layer">
    <button className="drawer-scrim" aria-label="关闭待办详情" onClick={close}/>
    <aside className="drawer" role="dialog" aria-modal="true" aria-labelledby="pending-detail-title" tabIndex={-1} ref={dialogRef}>
      <header><div><small>待办处理详情 · #{id}</small><h2 id="pending-detail-title">{cleaned ? "已清理待办" : detail?.title || "待办详情"}</h2></div><button className="icon-button" aria-label="关闭" onClick={close}><X size={19}/></button></header>
      <div className="drawer-body">
        {error && <div className="error-banner" role="alert">{error}<button onClick={() => void loadDetail()}>重新读取</button></div>}
        {message && <div className="settings-message" role="status">{message}</div>}
        {!detail && <p className="empty">{error ? "暂时无法读取详情" : "正在读取详情…"}</p>}
        {detail && <>
          {cleaned ? <p className="notice">本地待办内容已清理，仅保留处理状态。清理时间：{time(detail.cleaned_at)}</p> : <div className="detail-grid"><div className="info"><span>发起人</span><strong>{detail.sender || "-"}</strong></div><div className="info"><span>当前节点</span><strong>{detail.current_node || "-"}</strong></div></div>}
          <h3>处理进度</h3>
          <div className="evidence-list">{STAGES.map(([key, label]) => <div key={key}><span>{label}</span><Status value={detail.stages?.[key] || "pending"}/></div>)}</div>
          <div className="detail-grid"><div className="info"><span>飞书投递</span><Status value={detail.feishu_status}/></div><div className="info"><span>本地清理</span><Status value={detail.cleanup_status}/></div></div>
          {reconcile && <p className="notice" role="status">飞书投递结果不确定，请先核对飞书是否已收到通知。为避免重复发送，当前不能直接重试投递或清理。</p>}
          {reconcile && !cleaned && detail.delivery_id && <div className="workflow-actions"><button disabled={busy || !!error} onClick={() => void reconcileDelivery("sent")}>核对后登记已收到</button><button disabled={busy || !!error} onClick={() => void reconcileDelivery("failed")}>核对后登记未收到</button></div>}
          {!cleaned && <><h3>智能摘要</h3>{detail.ollama_summary ? <div className="markdown-preview"><p>{detail.ollama_summary.summary}</p>{detail.ollama_summary.key_points?.length > 0 && <ul>{detail.ollama_summary.key_points.map((point, index) => <li key={index}>{point}</li>)}</ul>}{detail.ollama_summary.required_action && <p>建议处理：{detail.ollama_summary.required_action}</p>}</div> : <p className="settings-help">暂无保留的摘要正文，处理状态以上方进度为准。</p>}</>}
          {!cleaned && <div className="toolbar-actions">
            {detail.can_retry_summary && <button disabled={busy || !!error} onClick={() => void act("retry-summary")}>重试摘要</button>}
            {detail.can_retry_delivery && detail.feishu_status === "failed" && !reconcile && <button disabled={busy || !!error} onClick={() => void act("retry-delivery")}>重试飞书投递</button>}
            {detail.can_cleanup && !reconcile && <button disabled={busy || !!error} onClick={() => void act("cleanup")}>清理本地待办数据</button>}
          </div>}
        </>}
      </div>
    </aside>
  </div>
}

export function PendingView({ rows, total, page, pageSize = 50, setPage, query, setQuery, filter, setFilter, selectedId, onSelect, refresh }: {
  rows: PendingRow[]; total: number; page: number; pageSize?: number; setPage: (page: number) => void
  query: string; setQuery: (query: string) => void; filter: string; setFilter: (filter: string) => void
  selectedId: number | null; onSelect: (id: number | null) => void; refresh: () => Promise<void>
}) {
  const pages = Math.max(1, Math.ceil(total / pageSize))
  return <section>
    <div className="section-toolbar"><div><h2>待办通知</h2><p>查看待办摘要、飞书投递与本地清理进度。点击事项查看详情。</p></div><span>共 {total} 条</span></div>
    <div className="filter-row"><label className="search"><Search size={17}/><input aria-label="搜索待办" value={query} onChange={e => setQuery(e.target.value)} placeholder="搜索标题、发起人或节点"/></label><select aria-label="待办状态筛选" value={filter} onChange={e => setFilter(e.target.value)}>{FILTERS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></div>
    <div className="table-wrap"><table><thead><tr><th className="title-col">事项</th><th>当前节点</th><th>摘要</th><th>飞书</th><th>清理</th><th>最近发现</th></tr></thead><tbody>
      {rows.map(row => { const cleaned = row.cleanup_status === "cleaned" || row.occurrence_status === "cleaned"; return <tr key={row.id} tabIndex={0} onClick={() => onSelect(row.id)} onKeyDown={event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect(row.id) } }} aria-label={`查看${cleaned ? "已清理待办" : row.title || "待办"}详情`}><td className="title-cell"><strong>{cleaned ? "已清理待办" : row.title || "无标题待办"}</strong><small>{cleaned ? `记录 #${row.id}` : row.sender || "-"}</small></td><td>{cleaned ? "-" : row.current_node || "-"}</td><td><Status value={row.summary_status}/></td><td><Status value={row.feishu_status}/></td><td><Status value={row.cleanup_status}/></td><td>{time(row.last_seen_at || row.received_at)}</td></tr> })}
      {!rows.length && <tr><td colSpan={6} className="empty">没有符合条件的待办</td></tr>}
    </tbody></table></div>
    <div className="pagination"><span>第 {page} / {pages} 页</span><button disabled={page <= 1} onClick={() => setPage(page - 1)}>上一页</button><button disabled={page >= pages} onClick={() => setPage(page + 1)}>下一页</button></div>
    {selectedId !== null && <PendingDrawer key={selectedId} id={selectedId} close={() => onSelect(null)} refresh={refresh}/>}
  </section>
}
