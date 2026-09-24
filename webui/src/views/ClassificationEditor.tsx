import { useState } from "react"
import { postApi } from "../App"

const CATEGORIES = [
  "01_公司治理与决策", "02_业务项目与投放租后", "03_风险合规审计法务",
  "04_财务资金与融资", "05_经营计划与绩效考核", "06_人力资源",
  "07_党建纪检与工会", "08_行政采购与信息化", "09_对外报送与监管反馈",
  "99_其他内部",
]

export function ClassificationEditor({ itemId, sourceType, category, issuer, onSaved }: {
  itemId: number
  sourceType: string
  category: string | null
  issuer: string | null
  onSaved: () => Promise<void>
}) {
  const [origin, setOrigin] = useState<"internal" | "external">(sourceType === "external" ? "external" : "internal")
  const [businessCategory, setBusinessCategory] = useState(category || "")
  const [canonicalIssuer, setCanonicalIssuer] = useState(issuer || "")
  const [reason, setReason] = useState("")
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState("")
  async function save() {
    if (busy) return
    setBusy(true)
    setMessage("")
    try {
      await postApi(`/api/markdown-outputs/items/${itemId}/classification`, {
        content_origin: origin,
        business_category: origin === "internal" ? businessCategory : null,
        canonical_issuer: origin === "external" ? canonicalIssuer.trim() : null,
        reason: reason.trim(),
      })
      setMessage("分类已保存，已安排本条资料重新发布。")
      await onSaved()
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "保存分类失败")
    } finally {
      setBusy(false)
    }
  }
  return <details className="evidence-row">
    <summary>调整本条分类</summary>
    <p>只修改本条分类和本地交付，不重新下载原件。</p>
    <div className="filter-row">
      <label>文件性质 <select aria-label="文件性质" value={origin} onChange={event => setOrigin(event.target.value as "internal" | "external")}><option value="internal">内部事项</option><option value="external">外部文件</option></select></label>
      {origin === "internal"
        ? <label>业务分类 <select aria-label="业务分类" value={businessCategory} onChange={event => setBusinessCategory(event.target.value)}><option value="">请选择</option>{CATEGORIES.map(value => <option key={value} value={value}>{value}</option>)}</select></label>
        : <label>实际发文单位 <input aria-label="实际发文单位" value={canonicalIssuer} onChange={event => setCanonicalIssuer(event.target.value)} maxLength={80}/></label>}
    </div>
    <label>分类依据 <textarea aria-label="分类依据" value={reason} onChange={event => setReason(event.target.value)} maxLength={400} placeholder="请填写核对原件后的依据"/></label>
    <div className="workflow-actions"><button disabled={busy || reason.trim().length < 3 || (origin === "internal" ? !businessCategory : canonicalIssuer.trim().length < 2)} onClick={() => void save()}>{busy ? "正在保存…" : "保存分类"}</button></div>
    {message && <p role="status">{message}</p>}
  </details>
}
