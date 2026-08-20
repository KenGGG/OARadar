import { time } from "../App"

export type MarkdownItem = {
  id: number; title: string; source_type: string; internal_category: string | null; external_issuer: string | null
  markdown_count: number; delivery_status: string; index_relpath: string | null; source_relpath: string | null; updated_at: string | null
}

export function MarkdownView({ rows }: { rows: MarkdownItem[] }) {
  return <section>
    <div className="section-toolbar"><div><h2>Markdown 输出</h2><p>按已办事项聚合的 Source Markdown 交付状态。</p></div></div>
    <div className="table-wrap"><table><thead><tr><th>事项</th><th>分类</th><th>Markdown</th><th>交付状态</th><th>索引</th><th>最近更新</th></tr></thead><tbody>
      {rows.map(row => <tr key={row.id}><td className="title-cell"><strong>{row.title}</strong><small>{row.source_relpath || "-"}</small></td><td>{row.source_type === "internal" ? row.internal_category || "内部" : row.source_type === "external" ? row.external_issuer || "外部" : "未知"}</td><td>{row.markdown_count}</td><td>{row.delivery_status}</td><td>{row.index_relpath || "待生成"}</td><td>{time(row.updated_at)}</td></tr>)}
      {!rows.length && <tr><td colSpan={6} className="empty">尚无 Markdown 输出</td></tr>}
    </tbody></table></div>
  </section>
}
