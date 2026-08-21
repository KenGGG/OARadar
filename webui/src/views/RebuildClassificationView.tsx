import { useEffect, useRef, useState } from "react"
import { createPortal } from "react-dom"
import { CircleAlert, RefreshCw, X } from "lucide-react"
import { api, postApi, time } from "../App"
import type { ClassificationGroup, RebuildClassificationItem, RebuildClassificationPage, RebuildClassificationSummary, RebuildExecutionStatus, RebuildValidationReport } from "../types/rebuild"

const GROUPS: { id: ClassificationGroup; label: string }[] = [
  { id: "internal", label: "内部事项" },
  { id: "external", label: "外部事项" },
  { id: "needs_review", label: "待确认事项" },
]
const INTERNAL_CATEGORIES = [
  "公司治理", "经营管理", "业务项目", "风险管理",
  "财务资金", "人力行政", "信息化", "其他内部",
] as const
type InternalCategory = typeof INTERNAL_CATEGORIES[number]
type FormSourceType = "internal" | "external" | ""

function isInternalCategory(value: string): value is InternalCategory {
  return INTERNAL_CATEGORIES.includes(value as InternalCategory)
}

function groupLabel(group: ClassificationGroup) {
  return GROUPS.find(item => item.id === group)?.label || ""
}

function sourceTypeForForm(value: RebuildClassificationItem["source_type"]): FormSourceType {
  return value === "internal" || value === "external" ? value : ""
}

function metadataFor(item: RebuildClassificationItem) {
  return [
    item.document_number && `文号：${item.document_number}`,
    item.sender && `来源：${item.sender}`,
    item.item_date ? `日期：${time(item.item_date)}` : "日期待补充",
    item.has_document_number ? "主文档：需生成正文" : "主文档：无需生成正文",
    `附件：${item.attachment_count} 个`,
  ].filter(Boolean).join(" · ") || "暂无可展示的元数据"
}

function Dialog({ children, close, titleId }: { children: React.ReactNode; close: () => void; titleId: string }) {
  const closeRef = useRef<HTMLButtonElement>(null)
  const dialogRef = useRef<HTMLElement>(null)
  const previouslyFocused = useRef(
    document.activeElement instanceof HTMLElement ? document.activeElement : null,
  ).current
  useEffect(() => {
    const appShell = document.querySelector<HTMLElement>(".app-shell")
    const wasInert = appShell?.inert || false
    if (appShell) appShell.inert = true
    closeRef.current?.focus()
    return () => {
      if (appShell) appShell.inert = wasInert
      previouslyFocused?.focus()
    }
  }, [previouslyFocused])
  const getFocusableElements = () => Array.from(
    dialogRef.current?.querySelectorAll<HTMLElement>("button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [href], [tabindex]:not([tabindex='-1'])") || [],
  ).filter(element => element.tabIndex >= 0)
  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === "Escape") { event.preventDefault(); close(); return }
    if (event.key !== "Tab") return
    const focusable = getFocusableElements()
    if (!focusable.length) { event.preventDefault(); return }
    const currentIndex = focusable.indexOf(document.activeElement as HTMLElement)
    if (event.shiftKey && currentIndex <= 0) {
      event.preventDefault(); focusable[focusable.length - 1].focus()
    } else if (!event.shiftKey && (currentIndex === -1 || currentIndex === focusable.length - 1)) {
      event.preventDefault(); focusable[0].focus()
    }
  }
  return createPortal(<div className="classification-dialog-layer" role="presentation" onKeyDown={onKeyDown}>
    <button className="classification-dialog-scrim" aria-label="关闭确认窗口" onClick={close}/>
    <section ref={dialogRef} className="classification-dialog" role="dialog" aria-modal="true" aria-labelledby={titleId}>
      <button ref={closeRef} className="icon-button classification-dialog-close" aria-label="关闭" onClick={close}><X size={18}/></button>
      {children}
    </section>
  </div>, document.body)
}

export function RebuildClassificationView({ refreshKey }: { refreshKey: number }) {
  const [group, setGroup] = useState<ClassificationGroup>("internal")
  const [page, setPage] = useState(1)
  const [data, setData] = useState<RebuildClassificationPage | null>(null)
  const [summary, setSummary] = useState<RebuildClassificationSummary | null>(null)
  const [execution, setExecution] = useState<RebuildExecutionStatus | null>(null)
  const [validation, setValidation] = useState<RebuildValidationReport | null>(null)
  const [selected, setSelected] = useState<RebuildClassificationItem | null>(null)
  const [bulkGroup, setBulkGroup] = useState<"internal" | "external" | null>(null)
  const [sourceType, setSourceType] = useState<"internal" | "external" | "">("")
  const [internalCategory, setInternalCategory] = useState<InternalCategory | "">("")
  const [externalIssuer, setExternalIssuer] = useState("")
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")

  const load = async () => {
    setLoading(true); setError("")
    try {
      const [nextData, nextSummary, nextExecution, nextValidation] = await Promise.all([
        api<RebuildClassificationPage>(`/api/rebuild/classifications?group=${group}&page=${page}&page_size=50`),
        api<RebuildClassificationSummary>("/api/rebuild/classification-summary"),
        api<RebuildExecutionStatus>("/api/rebuild/status"),
        api<RebuildValidationReport>("/api/rebuild/validation"),
      ])
      setData(nextData); setSummary(nextSummary); setExecution(nextExecution); setValidation(nextValidation)
    } catch (reason) { setError(reason instanceof Error ? reason.message : "无法读取分类复核数据") }
    finally { setLoading(false) }
  }

  useEffect(() => { void load() }, [group, page, refreshKey])

  const openConfirmation = (item: RebuildClassificationItem) => {
    const suggestedCategory = item.internal_category || ""
    setSelected(item)
    setSourceType(sourceTypeForForm(item.source_type))
    setInternalCategory(isInternalCategory(suggestedCategory) ? suggestedCategory : "")
    setExternalIssuer(item.external_issuer || "")
  }
  const valid = sourceType === "internal"
    ? isInternalCategory(internalCategory)
    : sourceType === "external" && externalIssuer.trim().length > 0
  const pages = Math.max(1, Math.ceil((data?.total || 0) / 50))

  const confirmOne = async () => {
    if (!selected || !valid || !sourceType) return
    setBusy(true); setError("")
    try {
      await postApi(`/api/rebuild/classifications/${selected.id}/confirm`, {
        source_type: sourceType,
        internal_category: sourceType === "internal" && isInternalCategory(internalCategory) ? internalCategory : null,
        external_issuer: sourceType === "external" ? externalIssuer.trim() : null,
      })
      await load(); setBusy(false); setSelected(null)
    } catch (reason) { setError(reason instanceof Error ? reason.message : "确认失败") }
    finally { setBusy(false) }
  }
  const confirmBulk = async () => {
    if (!bulkGroup) return
    setBusy(true); setError("")
    try {
      await postApi("/api/rebuild/classifications/bulk-confirm", { source_type: bulkGroup })
      await load(); setBusy(false); setBulkGroup(null)
    } catch (reason) { setError(reason instanceof Error ? reason.message : "批量确认失败") }
    finally { setBusy(false) }
  }
  const seedSuggestions = async () => {
    setBusy(true); setError("")
    try { await postApi("/api/rebuild/classifications/suggest"); await load() }
    catch (reason) { setError(reason instanceof Error ? reason.message : "生成建议失败") }
    finally { setBusy(false) }
  }

  return <section className="classification-review">
    <div className="classification-intro">
      <div><h2>资料重建分类复核</h2><p>仅展示事项标题和归档元数据；请逐项确认待确认事项。</p></div>
      <button className="toolbar-button" disabled={busy || loading} onClick={() => void seedSuggestions()}><RefreshCw size={15}/>生成分类建议</button>
    </div>
    {error && <div className="notice classification-notice" role="alert"><CircleAlert size={17}/><span>{error}</span></div>}
    <section className="classification-bulk" aria-labelledby="rebuild-progress-title">
      <div><strong id="rebuild-progress-title">Markdown 重建进度</strong><span>排队 {execution?.queued || 0} · 执行中 {execution?.running || 0} · 已完成 {execution?.completed || 0} · 失败 {execution?.failed || 0}</span></div>
      <div className="classification-bulk-actions">
        <button className="toolbar-button" disabled title="需完成 Phase 4 CAS 修复后才能执行">开始重建</button>
        <button className="toolbar-button" disabled title="需完成 Phase 4 CAS 修复后才能执行">暂停</button>
        <button className="toolbar-button" disabled title="需完成 Phase 4 CAS 修复后才能执行">继续</button>
      </div>
    </section>
    <section className="classification-bulk" aria-labelledby="rebuild-validation-title">
      <div><strong id="rebuild-validation-title">重建验收</strong><span>{!validation ? "正在读取验收状态" : !validation.available ? "尚无可验证的重建运行" : validation.passed ? `全部 ${validation.checks.length} 项验收已通过` : `未通过 ${validation.checks.filter(check => !check.ok).length}/${validation.checks.length} 项验收`}</span></div>
      {validation?.available && !validation.passed && <div className="classification-bulk-actions" aria-label="未通过的验收项目">{validation.checks.filter(check => !check.ok).map(check => <span className="status status-warn" key={check.code}>{check.code}</span>)}</div>}
    </section>
    <div className="notice classification-notice" role="status"><CircleAlert size={17}/><span>安全门已启用：生产环境不从 Web 控制台执行重建；仅在本机验证和明确授权后由受限命令执行。</span></div>
    <div className="classification-tabs" role="tablist" aria-label="分类复核分组">
      {GROUPS.map(item => <button key={item.id} role="tab" aria-selected={group === item.id} className={group === item.id ? "classification-tab-active" : ""} onClick={() => { setGroup(item.id); setPage(1) }}>
        {item.label}<span>{item.id === "needs_review" ? summary?.needs_review.total || 0 : summary?.[item.id].total || 0}</span>
      </button>)}
    </div>
    {group !== "needs_review" && <div className="classification-bulk">
      <div><strong>{summary?.[group].suggested || 0}</strong><span> 项高置信度建议尚未确认</span></div>
      <button className="toolbar-button" disabled={busy || !(summary?.[group].suggested)} onClick={() => setBulkGroup(group)}>
        {group === "internal" ? "确认全部明确的内部事项" : "确认全部明确的外部事项"}
      </button>
    </div>}
    {loading ? <div className="classification-loading"><RefreshCw className="spin"/>正在读取本地分类数据</div> : <>
      <div className="table-wrap"><table className="classification-table"><thead><tr><th>事项标题</th><th>归档元数据</th><th>当前分类</th><th>状态</th><th aria-label="操作"/></tr></thead><tbody>
        {data?.items.map(item => <tr key={item.id}><td className="title-cell"><strong>{item.title || "未命名事项"}</strong></td><td>{metadataFor(item)}</td><td>{item.source_type === "internal" ? item.internal_category || "内部事项" : item.source_type === "external" ? item.external_issuer || "外部事项" : "待人工判断"}</td><td><span className={`status status-${item.classification_state === "confirmed" ? "good" : "warn"}`}>{item.classification_state === "confirmed" ? "已确认" : item.classification_state === "suggested" ? "建议" : "待确认"}</span></td><td><button className="row-action" disabled={busy || item.classification_state === "confirmed"} onClick={() => openConfirmation(item)}>确认</button></td></tr>)}
        {!data?.items.length && <tr><td colSpan={5} className="empty">{groupLabel(group)}暂无事项</td></tr>}
      </tbody></table></div>
      <div className="pagination"><button disabled={page <= 1 || busy} onClick={() => setPage(page - 1)}>上一页</button><span>第 {page}/{pages} 页 · 共 {(data?.total || 0).toLocaleString()} 项</span><button disabled={page >= pages || busy} onClick={() => setPage(page + 1)}>下一页</button></div>
    </>}
    {selected && <Dialog close={() => !busy && setSelected(null)} titleId="classification-confirm-title">
      <header><div><small>确认分类</small><h3 id="classification-confirm-title">{selected.title || "未命名事项"}</h3></div></header>
      <p className="classification-dialog-meta">{metadataFor(selected)}</p>
      <div className="classification-form"><label><span>事项类别</span><select value={sourceType} onChange={event => setSourceType(event.target.value as FormSourceType)} disabled={busy}><option value="">请选择事项类别</option><option value="internal">内部事项</option><option value="external">外部事项</option></select></label>
        {sourceType === "internal" ? <label><span>内部分类</span><select value={internalCategory} onChange={event => setInternalCategory(event.target.value as InternalCategory | "")} disabled={busy}><option value="">请选择内部分类</option>{INTERNAL_CATEGORIES.map(category => <option key={category} value={category}>{category}</option>)}</select></label> : sourceType === "external" ? <label><span>外部机构</span><input value={externalIssuer} onChange={event => setExternalIssuer(event.target.value)} placeholder="填写外部发文机构" disabled={busy}/></label> : null}
      </div>
      <footer><button className="secondary-button" disabled={busy} onClick={() => setSelected(null)}>取消</button><button className="primary-button" disabled={busy || !valid} onClick={() => void confirmOne()}>{busy ? "正在确认…" : "确认分类"}</button></footer>
    </Dialog>}
    {bulkGroup && <Dialog close={() => !busy && setBulkGroup(null)} titleId="classification-bulk-title">
      <header><div><small>批量确认</small><h3 id="classification-bulk-title">确认高置信度分类建议</h3></div></header>
      <p>将确认全部当前显示为“建议”的{groupLabel(bulkGroup)}。待确认事项不会被修改。</p>
      <footer><button className="secondary-button" disabled={busy} onClick={() => setBulkGroup(null)}>取消</button><button className="primary-button" disabled={busy} onClick={() => void confirmBulk()}>{busy ? "正在确认…" : "确认批量操作"}</button></footer>
    </Dialog>}
  </section>
}
