import { useEffect, useState } from "react"
import { ChevronRight, CircleAlert, Search, X } from "lucide-react"
import type { SimpleDoneItem, SimpleDoneState, SimpleDonePage } from "../types/simple-status"
import { time } from "../App"

type Tone = "good" | "warn" | "bad" | "neutral"

const DONE_FILTERS: { key: SimpleDoneState | ""; label: string }[] = [
  { key: "", label: "全部" },
  { key: "waiting_download", label: "等待下载" },
  { key: "waiting_markdown", label: "等待 MD 化" },
  { key: "waiting_classification", label: "等待归类" },
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

// 由简化状态推导“原件 / Markdown / 归类发布”三件事的口语结论（spec §6.2）。
function progressFacts(item: SimpleDoneItem): { original: string; markdown: string; publish: string } {
  const s = item.simple_status
  const original =
    item.pipeline_status === "downloaded" || item.pipeline_status === "no_attachment" ? "已验证"
    : item.pipeline_status === "download_failed" || item.pipeline_status === "partial" ? "不完整"
    : "等待下载"
  let markdown = "待生成"
  let publish = "未开始"
  if (s === "waiting_classification" || s === "completed" || (s === "attention" && item.attention_reason?.includes("归类"))) {
    markdown = "已生成"
    publish = s === "completed" ? "已发布" : "进行中"
  } else if (s === "attention") {
    markdown = "需人工确认"
    publish = "需人工确认"
  }
  return { original, markdown, publish }
}

function SimpleDoneDrawer({ item, close }: {
  item: SimpleDoneItem
  close: () => void
}) {
  const facts = progressFacts(item)
  const isAttention = item.simple_status === "attention"
  return <div className="drawer-layer" role="dialog" aria-modal="true">
    <button className="drawer-scrim" aria-label="关闭详情" onClick={close}/>
    <aside className="drawer">
      <header>
        <div><small>已办事项</small><h2>{item.title}</h2></div>
        <button className="icon-button" title="关闭" onClick={close}><X size={19}/></button>
      </header>
      <div className="drawer-body">
        <div className="detail-grid">
          <div className="info"><span>当前状态</span><strong>{item.simple_status_label}</strong></div>
          <div className="info"><span>原件</span><strong>{facts.original}</strong></div>
          <div className="info"><span>Markdown</span><strong>{facts.markdown}</strong></div>
          <div className="info"><span>归类发布</span><strong>{facts.publish}</strong></div>
          <div className="info"><span>附件数量</span><strong>{item.file_count == null ? "尚未取得" : String(item.file_count)}</strong></div>
          <div className="info"><span>发起人</span><strong>{item.sender || "-"}</strong></div>
          <div className="info"><span>发起时间</span><strong>{time(item.initiated_at)}</strong></div>
          <div className="info"><span>最近成功同步</span><strong>{time(item.updated_at)}</strong></div>
        </div>
        <h3>附件名称</h3>
        {item.attachment_names.length ? <div className="attachment-name-list">
          {item.attachment_names.map(name => <div key={name}>{name}</div>)}
        </div> : <p className="settings-help">暂无附件。</p>}
        {isAttention && item.attention_reason && (
          <div className="settings-message" role="alert"><CircleAlert size={16}/>{item.attention_reason}</div>
        )}
        <details className="advanced"><summary>查看技术详情</summary>
          <div className="detail-grid">
            <div className="info"><span>OA 事项 ID</span><strong>{item.item_id || "-"}</strong></div>
            <div className="info"><span>内部处理状态</span><strong>{item.pipeline_status}</strong></div>
            <div className="info"><span>本地归档目录</span><strong>{item.archive_relpath || "尚未归档"}</strong></div>
          </div>
        </details>
      </div>
    </aside>
  </div>
}

export function SimpleDoneView({ rows, total, metrics, page, setPage, query, setQuery, filter, setFilter }: {
  rows: SimpleDoneItem[]
  total: number
  metrics: SimpleDonePage["metrics"]
  page: number
  setPage: (p: number) => void
  query: string
  setQuery: (v: string) => void
  filter: SimpleDoneState | ""
  setFilter: (v: SimpleDoneState | "") => void
}) {
  const pages = Math.max(1, Math.ceil(total / 50))
  const [selected, setSelected] = useState<SimpleDoneItem | null>(null)
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

  return <section className="done-page">
    <div className="metrics compact-metrics">
      <div className="metric"><span>已办总数</span><strong>{metrics.oa_done_total.toLocaleString()}</strong></div>
      <div className="metric"><span>成功下载</span><strong>{metrics.downloaded_items.toLocaleString()}</strong></div>
      <div className="metric"><span>已验证附件</span><strong>{metrics.verified_attachments.toLocaleString()}</strong></div>
    </div>
    <div className="section-toolbar"><div><h2>已办资料</h2><p>原件、Markdown 与归类发布的当前完成情况。</p></div></div>
    <div className="filter-row">
      <label className="search"><Search size={17}/><input value={query} onChange={e => setQuery(e.target.value)} placeholder="搜索事项标题"/></label>
      <select value={filter} onChange={e => setFilter(e.target.value as SimpleDoneState | "")}>
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
        <tr key={row.id} onClick={() => setSelected(row)} tabIndex={0} onKeyDown={e => e.key === "Enter" && setSelected(row)}>
          <td className="title-cell"><strong>{row.title}</strong></td>
          <td>{row.sender || "-"}</td>
          <td className="nowrap">{time(row.initiated_at)}</td>
          <td>{row.file_count == null ? "-" : row.file_count}</td>
          <td><span className={`status status-${statusTone(row.simple_status)}`}>{row.simple_status_label}</span></td>
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
    {selected && <SimpleDoneDrawer item={selected} close={() => setSelected(null)}/>}
  </section>
}
