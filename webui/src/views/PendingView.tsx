import { useState } from "react"
import { Search } from "lucide-react"
import { postApi, time } from "../App"

export type PendingRow = {
  id: number; title: string | null; sender: string | null; current_node: string | null
  received_at: string | null; summary_status: string; feishu_status: string; cleanup_status: string
}

export function PendingView({ rows, refresh }: { rows: PendingRow[]; refresh: () => void }) {
  const [query, setQuery] = useState("")
  const [busy, setBusy] = useState<number | null>(null)
  const visible = rows.filter(row => `${row.title || ""} ${row.sender || ""}`.includes(query))
  async function retrySummary(id: number) {
    setBusy(id)
    try { await postApi(`/api/pending-notifications/${id}/retry-summary`); refresh() }
    finally { setBusy(null) }
  }
  return <section>
    <div className="section-toolbar"><div><h2>待办通知</h2><p>仅处理短生命周期的待办摘要、飞书投递与清理。</p></div></div>
    <div className="filter-row"><label className="search"><Search size={17}/><input value={query} onChange={e => setQuery(e.target.value)} placeholder="搜索待办"/></label></div>
    <div className="table-wrap"><table><thead><tr><th>事项</th><th>当前节点</th><th>摘要</th><th>飞书</th><th>清理</th><th>最近发现</th><th/></tr></thead><tbody>
      {visible.map(row => <tr key={row.id}><td className="title-cell"><strong>{row.title || "已清理待办"}</strong><small>{row.sender || "-"}</small></td><td>{row.current_node || "-"}</td><td>{row.summary_status}</td><td>{row.feishu_status}</td><td>{row.cleanup_status}</td><td>{time(row.received_at)}</td><td>{row.summary_status === "failed" && <button disabled={busy === row.id} onClick={() => void retrySummary(row.id)}>重试摘要</button>}</td></tr>)}
      {!visible.length && <tr><td colSpan={7} className="empty">没有符合条件的待办</td></tr>}
    </tbody></table></div>
  </section>
}
