import { useEffect, useRef, useState } from "react"
import { X } from "lucide-react"
import { api, postApi, time, size } from "../App"

type Delivery = { status: string; expected: number; successful: number; unsupported: number; failed: number; index_status: string; classification_status: string; reason: string | null }
type Detail = {
  id: number | null; manifest_id: number | null; title: string; archive_status?: string; archive_error?: string | null; failure_stage?: string | null
  no_attachment_confirmed?: boolean; delivery: Delivery | null; delivery_status?: string; can_retry?: boolean; can_retry_archive?: boolean; retry_blocked_reason?: string | null
  files: { id: number; name: string; role: string; status: string; size_bytes: number | null; sha256: string | null; verified_at: string | null; download_url: string | null }[]
  documents: { id: number; name: string; kind: string; relpath: string; status: string; engine: string; error_code: string | null; error: string | null; generated_at: string | null }[]
  tasks: { id: number; stage: string; status: string; error: string | null; error_code: string | null; attempts: number; next_retry_at: string | null; updated_at: string | null }[]
}
const LABELS: Record<string, string> = { downloaded: "原件已验证", no_attachment: "无附件", download_failed: "归档失败", partial: "部分完成", auth_required: "需要登录 OA", depth_limit_reached: "达到层级上限，需人工核对", discovered: "等待归档", pending_download: "等待下载", processing: "处理中", scanning: "扫描中", skipped: "已排除", verified: "已验证", success: "已生成", failed: "失败", queued: "已排队", pending: "待处理", completed: "已完成", running: "处理中", classified: "已分类", needs_review: "待复核", excluded: "已排除", unknown: "尚未分类", unsupported: "不支持转换", attachment_inventory: "核对附件", parse: "转换正文", source_publish: "生成 Markdown", classify: "分类", index_publish: "生成索引", archive_verify: "校验原件", done_capture_and_archive: "采集原件" }
export const workflowLabel = (value: string) => LABELS[value] || "状态待确认"

export function WorkflowDrawer({ kind, id, close, refresh, onRelated }: { kind: "done" | "markdown"; id: number; close: () => void; refresh: () => Promise<void>; onRelated: (id: number) => void }) {
  const [detail, setDetail] = useState<Detail | null>(null)
  const [error, setError] = useState("")
  const [message, setMessage] = useState("")
  const [busy, setBusy] = useState(false)
  const [preview, setPreview] = useState<{ text: string; truncated: boolean } | null>(null)
  const ref = useRef<HTMLElement>(null)
  const closeRef = useRef(close); closeRef.current = close
  const serial = useRef(0)
  async function load() {
    const version = ++serial.current
    try {
      const result = await api<Detail>(kind === "done" ? `/api/done-archives/${id}` : `/api/markdown-outputs/items/${id}`)
      if (version === serial.current) { setDetail(result); setError("") }
    } catch (reason) { if (version === serial.current) setError(reason instanceof Error ? reason.message : "读取详情失败") }
  }
  useEffect(() => {
    void load(); const timer = window.setInterval(() => void load(), 5000)
    return () => { serial.current++; window.clearInterval(timer) }
  }, [kind, id])
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    ref.current?.focus()
    const key = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeRef.current()
      if (event.key === "Tab") {
        const controls = Array.from(ref.current?.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], [tabindex="0"]') || [])
        const first = controls[0], last = controls[controls.length - 1]
        if (event.shiftKey && (document.activeElement === first || document.activeElement === ref.current)) { event.preventDefault(); last?.focus() }
        else if (!event.shiftKey && (document.activeElement === last || document.activeElement === ref.current)) { event.preventDefault(); first?.focus() }
      }
    }
    document.addEventListener("keydown", key)
    return () => { document.removeEventListener("keydown", key); previous?.focus() }
  }, [])
  async function retry() {
    if (!detail || busy) return
    setBusy(true); setMessage("")
    try {
      await postApi(kind === "done" ? `/api/done-archives/${id}/retry-archive` : `/api/markdown-outputs/items/${id}/retry`)
      setMessage("任务已提交，进度会自动更新。")
      await load(); await refresh()
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : "操作失败") }
    finally { setBusy(false) }
  }
  async function showPreview(exportId: number) {
    setPreview(null)
    try { setPreview(await api(`/api/markdown-outputs/documents/${exportId}/content`)) }
    catch (reason) { setMessage(reason instanceof Error ? reason.message : "预览失败") }
  }
  return <div className="drawer-layer">
    <button className="drawer-scrim" aria-label="关闭详情" onClick={close}/>
    <aside className="drawer workflow-drawer" role="dialog" aria-modal="true" aria-label={kind === "done" ? "原件归档详情" : "Markdown 交付详情"} tabIndex={-1} ref={ref}>
      <header><div><small>{kind === "done" ? "原件归档" : "Markdown 交付"}</small><h2>{detail?.title || "正在读取详情"}</h2></div><button className="icon-button" title="关闭" onClick={close}><X size={18}/></button></header>
      <div className="drawer-body">
        {error && <p className="error-banner" role="alert">{error}</p>}
        {message && <p className="settings-message" role="status">{message}</p>}
        {detail && <>
          <div className="detail-grid">
            {detail.archive_status && <div className="info"><span>原件归档</span><strong>{detail.archive_status === "no_attachment" ? detail.no_attachment_confirmed ? "已核验无附件" : "未发现附件，待核实" : workflowLabel(detail.archive_status)}</strong></div>}
            {detail.delivery && <><div className="info"><span>Markdown</span><strong>{detail.delivery_status}</strong></div><div className="info"><span>附件交付</span><strong>{detail.delivery.successful} / {detail.delivery.expected}</strong></div><div className="info"><span>分类</span><strong>{workflowLabel(detail.delivery.classification_status)}</strong></div><div className="info"><span>事项索引</span><strong>{workflowLabel(detail.delivery.index_status)}</strong></div></>}
          </div>
          {(detail.archive_error || detail.delivery?.reason) && <p className="settings-message">{detail.archive_error || detail.delivery?.reason}</p>}
          <div className="workflow-actions">
            {(kind === "done" ? detail.can_retry_archive : detail.can_retry) && <button disabled={busy || !!error} onClick={() => void retry()}>{busy ? "正在提交…" : kind === "done" ? "重试归档" : detail.delivery?.status === "complete" ? "重新生成本地交付" : "重试本地交付"}</button>}
            {(kind === "done" ? detail.id : detail.manifest_id) != null && <button onClick={() => onRelated((kind === "done" ? detail.id : detail.manifest_id)!)}>{kind === "done" ? "查看 Markdown 交付" : "查看原件归档"}</button>}
          </div>
          {kind === "markdown" && detail.retry_blocked_reason && <p>{detail.retry_blocked_reason}</p>}
          {kind === "done" ? <><h3>原件清单</h3>{detail.files.map(file => <div className="evidence-row" key={file.id}><strong>{file.name}</strong><span>{workflowLabel(file.status)} · {size(file.size_bytes)}</span><small>校验时间：{time(file.verified_at)}</small>{file.download_url && <a href={file.download_url}>下载原件</a>}<details><summary>校验摘要</summary><code>{file.sha256 || "尚无校验记录"}</code></details></div>)}{!detail.files.length && <p>尚无已登记的原件。</p>}</> : <><h3>输出文件</h3>{detail.documents.map(doc => <div className="evidence-row" key={doc.id}><strong>{doc.name}</strong><span>{workflowLabel(doc.status)} · {doc.engine}</span><small>{doc.relpath}</small>{doc.error && <p>{doc.error}</p>}{doc.status === "success" && <div className="workflow-actions"><button onClick={() => void showPreview(doc.id)}>预览 {doc.kind === "item_index" ? "索引" : "正文"}</button><a href={`/api/markdown-outputs/documents/${doc.id}/download`}>下载 Markdown</a></div>}</div>)}{!detail.documents.length && <p>尚未生成输出文件。</p>}{preview && <section><h3>Markdown 预览</h3>{preview.truncated && <p>仅显示前 200,000 字符，可下载完整文件。</p>}<pre className="markdown-preview">{preview.text}</pre></section>}</>}
          <h3>最近处理记录</h3>{detail.tasks.map(task => <div className="evidence-row" key={task.id}><strong>{workflowLabel(task.stage)} · {workflowLabel(task.status)}</strong><small>最近更新：{time(task.updated_at)} · 已尝试 {task.attempts} 次</small>{task.error && <p>{task.error}</p>}{task.next_retry_at && <small>下次重试：{time(task.next_retry_at)}</small>}</div>)}{!detail.tasks.length && <p>暂无处理任务。</p>}
        </>}
      </div>
    </aside>
  </div>
}
