import { Search } from "lucide-react"
import { time } from "../App"
import { WorkflowDrawer } from "./WorkflowDrawer"

export type MarkdownItem = {
  id: number; title: string; source_type: string; internal_category: string | null; external_issuer: string | null
  archive_category_id?: string | null
  markdown_count: number; delivery_status: string; index_relpath: string | null; source_relpath: string | null; updated_at: string | null
  delivery: { successful: number; expected: number; unsupported: number; failed: number; status: string }
}
const SOURCE_LABELS: Record<string,string> = {SELF:"本公司资料",GROUP:"集团及成员企业",EXTERNAL:"外部机构来文",PUBLICATION:"刊物与学习资料",UNKNOWN:"待确认",EXCLUDED:"已排除"}

export function MarkdownView({ rows, total, page, setPage, query, setQuery, filter, setFilter, selectedId, onSelect, refresh, onArchive }: {
  rows: MarkdownItem[]; total: number; page: number; setPage: (page: number) => void
  query: string; setQuery: (query: string) => void; filter: string; setFilter: (filter: string) => void
  selectedId: number | null; onSelect: (id: number | null) => void; refresh: () => Promise<void>; onArchive: (id: number) => void
}) {
  const pages = Math.max(1, Math.ceil(total / 50))
  return <section>
    <div className="section-toolbar"><div><h2>Markdown 输出</h2><p>每份附件对应一份 Markdown，按文件来源归档。</p></div></div>
    <div className="filter-row"><label className="search"><Search size={17}/><input aria-label="搜索 Markdown 事项" value={query} onChange={e => setQuery(e.target.value)} placeholder="搜索标题或发文单位"/></label><select aria-label="Markdown 状态或来源分类" value={filter} onChange={e => setFilter(e.target.value)}><optgroup label="交付状态">{[["", "全部"], ["pending", "待处理"], ["working", "处理中"], ["complete", "完整交付"], ["partial", "部分交付"], ["failed", "交付失败"], ["needs_review", "待复核"], ["excluded", "已排除"]].map(([value,label]) => <option key={value} value={value}>{label}</option>)}</optgroup><optgroup label="来源分类">{Object.entries(SOURCE_LABELS).filter(([key]) => key !== "EXCLUDED").map(([key,label]) => <option key={key} value={`category:${key}`}>{label}</option>)}</optgroup></select></div>
    <div className="table-wrap"><table><thead><tr><th>事项</th><th>分类</th><th>附件交付</th><th>交付状态</th><th>最近更新</th></tr></thead><tbody>
      {rows.map(row => <tr key={row.id} tabIndex={0} onClick={() => onSelect(row.id)} onKeyDown={e => e.key === "Enter" && onSelect(row.id)}><td className="title-cell"><strong>{row.title}</strong></td><td>{SOURCE_LABELS[row.archive_category_id || ""] || (row.source_type === "internal" ? row.internal_category || "内部" : row.source_type === "external" ? row.external_issuer || "外部" : "待分类")}</td><td>{row.delivery?.successful ?? row.markdown_count} / {row.delivery?.expected ?? "—"}{!!row.delivery?.unsupported && <small> · 不支持 {row.delivery.unsupported}</small>}</td><td>{row.delivery_status}</td><td>{time(row.updated_at)}</td></tr>)}
      {!rows.length && <tr><td colSpan={5} className="empty">没有符合条件的 Markdown 事项</td></tr>}
    </tbody></table></div>
    <div className="pagination"><button disabled={page <= 1} onClick={() => setPage(page - 1)}>上一页</button><span>第 {page}/{pages} 页 · 共 {total.toLocaleString()} 项</span><button disabled={page >= pages} onClick={() => setPage(page + 1)}>下一页</button></div>
    {selectedId !== null && <WorkflowDrawer key={selectedId} kind="markdown" id={selectedId} close={() => onSelect(null)} refresh={refresh} onRelated={onArchive}/>}
  </section>
}
