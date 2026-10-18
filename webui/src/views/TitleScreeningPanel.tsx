import { useEffect, useState } from "react"
import { api, postApi } from "../App"

type Row = { id: number; title: string; item_id: string; action: string; label: string; rule_id: string;
  reason: string; folder: string; organizations: string[]; organization_basis: string[];
  classification_status: string; source_evidence_level?: string; source_relationship?: string; archive_bucket?: string; manual_reason?: string; original_status: string }
type State = { plan: { id: number; version: string; active: boolean; mode?: string } | null;
  category_counts?: Record<string, number>; classification_version?: string; counts: Record<string, number>; total: number; items: Row[] }
type Evidence = { evidence: { body: string; attachment_names: string[]; warning: string; suggestion?: {reason: string} } | null;
  history: {id: number; action: string; reason: string; at: string}[] }
type Job = { id: number; status: string; current: number; total: number; pause_requested: boolean }

export function TitleScreeningPanel({ refresh, onSelect }: { refresh: () => Promise<void>; onSelect: (id: number) => void }) {
  const [state, setState] = useState<State | null>(null)
  const [category, setCategory] = useState("")
  const [filter, setFilter] = useState("")
  const [query, setQuery] = useState("")
  const [search, setSearch] = useState("")
  const [page, setPage] = useState(1)
  const [revision, setRevision] = useState(0)
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)
  const [selected, setSelected] = useState<Row | null>(null)
  const [action, setAction] = useState("download")
  const [reason, setReason] = useState("")
  const [confirmFolder, setConfirmFolder] = useState(false)
  const [folder, setFolder] = useState("")
  const [evidence, setEvidence] = useState<Evidence | null>(null)
  const [reviewJob, setReviewJob] = useState<Job | null>(null)
  const [jobBusy, setJobBusy] = useState(false)
  const [message, setMessage] = useState("")
  useEffect(() => {
    let live = true
    const poll = () => { void api<{review: Job | null; inventory: Job | null; download: Job | null; login: Job | null}>("/api/done-collection").then(result => {
      if (live) { setReviewJob(result.review); setJobBusy([result.inventory, result.download, result.login, result.review].some(job => !!job && ["queued", "running"].includes(job.status))) }
    }).catch(() => {}) }
    poll(); const timer = window.setInterval(poll, 3000)
    return () => { live = false; window.clearInterval(timer) }
  }, [])
  useEffect(() => {
    let live = true
    const params = new URLSearchParams({ page: String(page), query: search })
    if (category) params.set("category", category)
    if (filter) params.set("action", filter)
    void api<State>(`/api/title-screening?${params}`).then(result => { if (live) setState(result) })
      .catch(reason => { if (live) setError(String(reason)) })
    return () => { live = false }
  }, [page, filter, category, search, revision])
  const mutate = async (path: string, body?: object) => {
    setBusy(true); setError("")
    try { const result = await postApi<{confirmed_indexes?: number}>(path, body); if (result.confirmed_indexes != null) setMessage(`已生成 ${result.confirmed_indexes} 项确认目录索引，原件保留原位。`); setRevision(value => value + 1); setSelected(null); await refresh() }
    catch (reason) { setError(String(reason)) }
    finally { setBusy(false) }
  }
  if (!state?.plan) return null
  return <details className="screening-panel">
    <summary>下载筛选与归档建议 · {state.plan.active ? "已启用" : "待启用"} · 排除 {state.counts.skip || 0} / 保留 {state.counts.download || 0} / 待复核 {state.counts.review || 0}</summary>
    <p className="collection-note">规则版本：{state.plan.version}。{state.plan.mode === "exclude_first" ? "六组排除规则依次匹配，命中即排除，其余默认下载；不使用保留词保护，也不自动安排正文复核。" : "待复核默认继续下载；本方案接管已办清单的旧标题黑名单。"}机构与路径独立判断，确认前不移动原件、不发布正式归档。</p>
    {state.classification_version && <p className="collection-note">五类分类版本：{state.classification_version} · {Object.entries(state.category_counts || {}).map(([key, count]) => `${({SELF:"本公司", GROUP:"集团及成员", EXTERNAL:"外部来文", PUBLICATION:"刊物与学习", UNKNOWN:"待确认", EXCLUDED:"排除", MANUAL:"人工分类"} as Record<string,string>)[key] || key} ${count}`).join("；")}</p>}
    {error && <p role="alert" className="collection-error">{error}</p>}
    {message && <p role="status">{message}</p>}
    <div className="screening-toolbar">
      {!state.plan.active && <button disabled={busy} onClick={() => void mutate(`/api/title-screening/${state.plan!.id}/activate`)}>启用此方案</button>}
      <a href="/api/title-screening/export.csv">导出全量筛选与归档建议</a>
      {!!state.counts.review && <button disabled={busy || jobBusy || !state.plan.active} onClick={() => void mutate(
        reviewJob && ["paused", "auth_required", "failed"].includes(reviewJob.status) ? `/api/done-collection/${reviewJob.id}/resume` : "/api/done-collection/start",
        reviewJob && ["paused", "auth_required", "failed"].includes(reviewJob.status) ? undefined : {operation: "done_review"})}>{reviewJob && ["paused", "auth_required", "failed"].includes(reviewJob.status) ? "继续正文预览" : "批量读取待复核正文与目录"}</button>}
      <button disabled={busy || jobBusy || !state.plan.active} onClick={() => void mutate("/api/title-screening/publish-index")}>生成已确认目录索引</button>
      <select aria-label="下载建议筛选" value={filter} onChange={event => { setFilter(event.target.value); setPage(1) }}>
        <option value="">全部建议</option><option value="skip">建议排除</option><option value="download">保留下载</option><option value="review">先看正文</option>
      </select>
      <select aria-label="归档类别筛选" value={category} onChange={event => {setCategory(event.target.value); setPage(1)}}><option value="">全部归档类别</option>{Object.entries({SELF:"本公司资料", GROUP:"集团及成员企业", EXTERNAL:"外部机构来文", PUBLICATION:"刊物与学习资料", UNKNOWN:"待确认", EXCLUDED:"排除，仅索引"}).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select>
      <input aria-label="搜索筛选建议" placeholder="标题或机构候选" value={query} onChange={event => setQuery(event.target.value)}/>
      <button onClick={() => { setSearch(query); setPage(1) }}>搜索</button>
    </div>
    {reviewJob && <div className="screening-toolbar"><span>正文预览：{reviewJob.current} / {reviewJob.total} 项 · {reviewJob.status === "completed" ? "已完成" : reviewJob.status === "paused" ? "已暂停" : reviewJob.status === "auth_required" ? "需要登录" : reviewJob.status === "failed" ? "需要重试" : "执行中"}</span><progress aria-label="正文预览进度" value={reviewJob.current} max={reviewJob.total || 1}/>
      {["running", "queued"].includes(reviewJob.status) && <button disabled={busy || reviewJob.pause_requested} onClick={() => void mutate(`/api/done-collection/${reviewJob.id}/pause`)}>暂停正文预览</button>}</div>}
    <p className="collection-note">正文预览只读取页面正文和可见附件标签，复用已校验正文；不会下载附件全文，也不会把未知数量记为零。</p>
    <div className="table-wrap"><table><thead><tr><th>标题与下载建议</th><th>机构与候选路径</th><th>复核</th></tr></thead>
      <tbody>{state.items.map(row => <tr key={row.id}>
        <td><strong>{row.title}</strong><p>{row.label} · {row.rule_id}</p><small>{row.manual_reason || row.reason}</small></td>
        <td><p>{row.organizations.join("；") || row.archive_bucket || "来源待确认"} · {row.classification_status === "confirmed" ? "人工已确认" : row.classification_status === "source_pending" ? "主题已归集，来源待核实" : row.classification_status === "classified" ? "规则已分类" : row.classification_status === "excluded" ? "仅索引" : "待确认"}</p><small>{row.organization_basis.join("；")}</small>{row.source_relationship === "multiple_documents" && <p>多份文件来源归集，尚未认定为联合发文</p>}<p className="screening-path">{row.folder}</p></td>
        <td><button onClick={() => onSelect(row.id)}>查看已有原件</button><button disabled={!state.plan?.active || jobBusy} onClick={() => {
          setSelected(row); setAction(row.action); setReason(""); setFolder(row.folder); setConfirmFolder(false)
          setEvidence(null); void api<Evidence>(`/api/title-screening/items/${row.id}/review`).then(setEvidence).catch(reason => setError(String(reason)))
        }}>正文复核 / 修改</button></td>
      </tr>)}</tbody></table></div>
    <div className="screening-toolbar"><button disabled={page <= 1} onClick={() => setPage(value => value - 1)}>上一页</button>
      <span>{page} / {Math.max(1, Math.ceil(state.total / 50))} 页 · {state.total} 项</span>
      <button disabled={page * 50 >= state.total} onClick={() => setPage(value => value + 1)}>下一页</button></div>
    {selected && <form className="screening-review" onSubmit={event => { event.preventDefault(); void mutate(`/api/title-screening/items/${selected.id}/override`, { action, reason, folder: confirmFolder ? folder : null }) }}>
      <strong>{selected.title}</strong>
      {evidence?.evidence ? <details open><summary>本地正文预览与附件标签</summary><p>{evidence.evidence.warning}</p><p>{evidence.evidence.suggestion?.reason}</p><pre className="screening-body">{evidence.evidence.body}</pre><p>{evidence.evidence.attachment_names.join("；") || "尚无可见附件标签，数量未知"}</p></details> : <p>尚无正文预览；可先批量读取待复核正文。复核前默认继续下载。</p>}
      {!!evidence?.history.length && <details><summary>人工修改记录</summary>{evidence.history.map(event => <p key={event.id}>{event.at} · {event.action} · {event.reason}</p>)}</details>}
      <label>人工处理意见<select value={action} onChange={event => setAction(event.target.value)}><option value="download">保留下载</option><option value="skip">仅索引，跳过下载</option><option value="review">待复核，继续下载</option><option value="inherit">恢复标题规则</option></select></label>
      <label>核对依据<textarea required minLength={3} maxLength={400} value={reason} onChange={event => setReason(event.target.value)} placeholder="请记录正文用途、落款或文号等依据"/></label>
      <label className="screening-confirm"><input type="checkbox" checked={confirmFolder} onChange={event => setConfirmFolder(event.target.checked)}/>已核实来源机构与目录，确认归档分类</label>
      {confirmFolder && <input aria-label="确认归档路径" required maxLength={220} value={folder} onChange={event => setFolder(event.target.value)}/>}
      <p className="collection-note">记录修改前后值和依据；现有原件不会被删除或移动。</p>
      <div className="screening-toolbar"><button disabled={busy}>保存复核意见</button><button type="button" onClick={() => setSelected(null)}>取消</button></div>
    </form>}
  </details>
}
