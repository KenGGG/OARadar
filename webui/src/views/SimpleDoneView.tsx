import { useEffect, useState } from "react"
import { ChevronRight, Download, Search } from "lucide-react"
import type { SimpleDoneFilter, SimpleDoneItem, SimpleDoneState, SimpleDonePage } from "../types/simple-status"
import { time } from "../App"
import { WorkflowDrawer } from "./WorkflowDrawer"

type Tone = "good" | "warn" | "bad" | "neutral"

const DONE_FILTERS: { key: SimpleDoneFilter | ""; label: string }[] = [
  { key: "", label: "全部" },
  { key: "no_attachment", label: "无附件事项" },
  { key: "waiting_download", label: "等待下载" },
  { key: "waiting_markdown", label: "等待 MD 化" },
  { key: "completed", label: "已完成" },
  { key: "attention", label: "需要处理" },
  { key: "excluded", label: "已按规则排除" },
]

function statusTone(state: SimpleDoneState): Tone {
  if (state === "completed") return "good"
  if (state === "attention") return "bad"
  if (state === "waiting_markdown" || state === "waiting_classification") return "warn"
  return "neutral"
}

const MARKDOWN_LABELS: Record<string, string> = {
  complete: "已交付", partial: "部分交付", failed: "失败", working: "处理中",
  pending: "待处理", needs_review: "待复核", excluded: "已排除",
}

const CLASSIFICATION_LABELS: Record<string, string> = {
  classified: "已分类", needs_review: "待确认", excluded: "已排除", unknown: "待分类",
}

function originalLabel(row: SimpleDoneItem): string {
  if (row.pipeline_status === "downloaded") return "已验证"
  if (row.pipeline_status === "no_attachment") return row.no_attachment_confirmed ? "已核验无附件" : "待核验无附件"
  return row.pipeline_status === "skipped" ? "已排除" : "待下载或核验"
}
export function SimpleDoneView({ rows, total, metrics, page, setPage, query, setQuery, filter, setFilter, selectedId, onSelect, refresh, onMarkdown }: {
  selectedId: number | null
  onSelect: (id: number | null) => void
  refresh: () => Promise<void>
  onMarkdown: (id: number) => void
  rows: SimpleDoneItem[]
  total: number
  metrics: SimpleDonePage["metrics"]
  page: number
  setPage: (p: number) => void
  query: string
  setQuery: (v: string) => void
  filter: SimpleDoneFilter | ""
  setFilter: (v: SimpleDoneFilter | "") => void
}) {
  const pages = Math.max(1, Math.ceil(total / 50))
  const [pageInput, setPageInput] = useState(String(page))

  useEffect(() => setPageInput(String(page)), [page])

  const goToPage = (requested: number) => {
    const target = Math.min(pages, Math.max(1, requested))
    setPage(target)
    setPageInput(String(target))
  }

  const submitPageJump = () => {
    const requested = Number.parseInt(pageInput, 10)
    goToPage(Number.isFinite(requested) ? requested : page)
  }

  const exportCsv = () => {
    const params = new URLSearchParams()
    if (query) params.set("query", query)
    if (filter === "no_attachment") params.set("attachment_review", filter)
    else if (filter) params.set("simple_status", filter)
    const suffix = params.size ? `?${params.toString()}` : ""
    window.location.assign(`/api/done-archives/export.csv${suffix}`)
  }

  return <section className="done-page">
    <div className="metrics compact-metrics">
      <div className="metric"><span>已办总数</span><strong>{metrics.oa_done_total.toLocaleString()}</strong></div>
      <div className="metric"><span>成功下载</span><strong>{metrics.downloaded_items.toLocaleString()}</strong></div>
      <div className="metric"><span>已验证附件</span><strong>{metrics.verified_attachments.toLocaleString()}</strong></div>
    </div>
    <div className="section-toolbar"><div><h2>已办资料</h2><p>查看已办原件的归档与校验结果，Markdown 交付单独追踪。</p></div>
      <button className="export-button" onClick={exportCsv}><Download size={16}/>导出 CSV</button>
    </div>
    <div className="filter-row">
      <label className="search"><Search size={17}/><input value={query} onChange={e => setQuery(e.target.value)} placeholder="搜索事项标题"/></label>
      <select value={filter} onChange={e => setFilter(e.target.value as SimpleDoneFilter | "")}>
        {DONE_FILTERS.map(f => <option key={f.key} value={f.key}>{f.label}</option>)}
      </select>
    </div>
    <div className="table-wrap done-table-wrap"><table style={{ minWidth: 920 }}><thead><tr>
      <th className="title-col">事项标题</th>
      <th>发起人</th>
      <th>发起时间</th>
      <th>附件数量</th>
      <th>当前状态</th>
      <th>最近成功同步</th>
      <th aria-label="操作"/>
    </tr></thead><tbody>
      {rows.map(row => (
        <tr key={row.id} onClick={() => onSelect(row.id)} tabIndex={0} onKeyDown={e => e.key === "Enter" && onSelect(row.id)}>
          <td className="title-cell"><strong>{row.title}</strong></td>
          <td>{row.sender || "-"}</td>
          <td className="nowrap">{time(row.initiated_at)}</td>
          <td className={row.pipeline_status === "no_attachment" ? "review-zero" : ""}>{row.attachment_review_label || (row.file_count == null ? "-" : row.file_count)}</td>
          <td><div style={{ display: "grid", gap: 4 }}><span className={`status status-${statusTone(row.simple_status)}`}>{row.simple_status_label}</span><small>原件：{originalLabel(row)} · Markdown：{MARKDOWN_LABELS[row.delivery?.status || "pending"] || "待处理"} · 分类：{CLASSIFICATION_LABELS[row.delivery?.classification_status || "unknown"] || "待确认"}</small></div></td>
          <td className="nowrap">{time(row.updated_at)}</td>
          <td><ChevronRight size={17}/></td>
        </tr>
      ))}
      {!rows.length && <tr><td colSpan={8} className="empty">没有符合条件的已办资料</td></tr>}
    </tbody></table></div>
    <div className="pagination">
      <button disabled={page <= 1} onClick={() => goToPage(page - 1)}>上一页</button>
      <span>第 {page}/{pages} 页 · 共 {total.toLocaleString()} 项</span>
      <label className="page-jump">
        <span>前往</span>
        <input aria-label="跳转页码" type="number" min="1" max={pages} inputMode="numeric" value={pageInput}
          onChange={event => setPageInput(event.target.value)}
          onKeyDown={event => event.key === "Enter" && submitPageJump()}/>
        <span>页</span>
      </label>
      <button onClick={submitPageJump}>跳转</button>
      <button disabled={page >= pages} onClick={() => goToPage(pages)}>末页</button>
      <button disabled={page >= pages} onClick={() => goToPage(page + 1)}>下一页</button>
    </div>
    {selectedId !== null && <WorkflowDrawer key={selectedId} kind="done" id={selectedId} close={() => onSelect(null)} refresh={refresh} onRelated={onMarkdown}/>}
  </section>
}
