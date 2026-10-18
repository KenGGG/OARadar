import { useEffect, useState } from "react"
import { api, postApi } from "../App"

type Rule = { id: string; name: string; action: string; pattern: string; note?: string; enabled?: boolean; unconditional_pattern?: string }
type Spec = { version: string; rules: Rule[]; custom_patterns: Record<string, string>; [key: string]: unknown }
type Settings = { plan_id: number | null; spec: Spec | null; history: { id: number; version: string; active: boolean }[] }
type Preview = { counts: Record<string, number>; changed: number; examples: { item_id: string; title: string; before: string; after: string }[] }

export function TitleRuleSettings({ onDirtyChange, onActive }: { onDirtyChange: (value: boolean) => void; onActive: (value: boolean) => void }) {
  const [state, setState] = useState<Settings | null>(null)
  const [draft, setDraft] = useState<Spec | null>(null)
  const [baseline, setBaseline] = useState("")
  const [preview, setPreview] = useState<Preview | null>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState("")
  const [error, setError] = useState("")
  const [historyId, setHistoryId] = useState("")
  const dirty = !!draft && JSON.stringify(draft) !== baseline
  useEffect(() => { onDirtyChange(dirty) }, [dirty, onDirtyChange])
  const load = async () => {
    const result = await api<Settings>("/api/title-screening/settings")
    setState(result); setDraft(result.spec); setBaseline(JSON.stringify(result.spec)); setPreview(null)
    onActive(result.plan_id !== null)
  }
  useEffect(() => { void load().catch(reason => setError(String(reason))) }, [])
  const updateRule = (index: number, patch: Partial<Rule>) => {
    setDraft(value => value && ({ ...value, rules: value.rules.map((rule, i) => i === index ? { ...rule, ...patch } : rule) }))
    setPreview(null); setMessage("")
  }
  const run = async (operation: "preview" | "save") => {
    if (!draft || !state?.plan_id) return
    setBusy(true); setError(""); setMessage("")
    try {
      const result = await postApi<Preview>(`/api/title-screening/settings/${operation}`, { base_plan_id: state.plan_id, spec: draft })
      if (operation === "save") { await load(); setMessage("新版本已保存并立即启用，后续下载使用新规则。已有原件及人工覆盖保留。") }
      else setPreview(result)
    } catch (reason) { setError(String(reason)) }
    finally { setBusy(false) }
  }
  return <div className="settings-sub settings-sub-card title-rule-settings">
    <h3>已办下载筛选规则</h3>
    {error && <p role="alert" className="error-banner">{error}</p>}
    {message && <p role="status" className="settings-message">{message}</p>}
    <fieldset disabled={busy} style={{border: 0, padding: 0, margin: 0, minWidth: 0}}>
    {!draft ? <p className="settings-help">{state ? "尚无启用的筛选方案。" : "正在读取当前规则…"}<button onClick={() => void load().catch(reason => setError(String(reason)))}>重新读取</button></p> : <>
      <p className="settings-help">当前版本：{state?.spec?.version}。按显示顺序首次命中生效；默认保留不可关闭。修改后先预演，再保存启用。规则独立保存，无需重启服务。</p>
      {draft.rules.map((rule, index) => <details key={rule.id} className="settings-advanced">
        <summary>{rule.id} · {rule.name} · {rule.enabled === false ? "已关闭" : rule.action}</summary>
        <div className="settings-sub">
          {rule.pattern !== "DEFAULT" && <label><input type="checkbox" checked={rule.enabled !== false} onChange={event => updateRule(index, {enabled: event.target.checked})}/>启用此规则</label>}
          <label>规则名称<input className="policy-edit-input" value={rule.name} onChange={event => updateRule(index, {name: event.target.value})}/></label>
          {!rule.pattern.startsWith("CUSTOM_") && rule.pattern !== "DEFAULT" && <label>标题匹配表达式<textarea className="settings-textarea" value={rule.pattern} onChange={event => updateRule(index, {pattern: event.target.value})}/><small>多个条件用 | 分隔；^ 表示标题开头。表达式是否有效会在预演时检查。</small></label>}
          {rule.unconditional_pattern != null && <label>无条件排除的标题开头表达式（留空取消此分支）<input className="policy-edit-input" value={rule.unconditional_pattern} onChange={event => updateRule(index, {unconditional_pattern: event.target.value})}/></label>}
          {rule.pattern.startsWith("CUSTOM_") && <p className="settings-help">组合匹配：流程词与用途词同时命中；若设置了无条件排除表达式，命中它也会排除。</p>}
          <label>规则说明<textarea className="settings-textarea" value={rule.note || ""} onChange={event => updateRule(index, {note: event.target.value})}/></label>
        </div>
      </details>)}
      <details className="settings-advanced"><summary>组合规则的流程词与用途词</summary>
        {Object.entries(draft.custom_patterns).map(([key, value]) => <label key={key}>{({workflow: "流程匹配词", admin_purpose: "后勤用途匹配词", event: "活动匹配词", business_event_guard: "业务活动保护词"} as Record<string,string>)[key] || key}
          <textarea className="settings-textarea" value={value} onChange={event => { setDraft({...draft, custom_patterns:{...draft.custom_patterns, [key]:event.target.value}}); setPreview(null); setMessage("") }}/></label>)}
      </details>
      <div className="policy-actions"><button disabled={busy} onClick={() => void run("preview")}>预演全部清单</button>
        <button className="button-primary" disabled={busy || !dirty || !preview} onClick={() => void run("save")}>保存并启用新版本</button>
        <button disabled={busy} onClick={() => void load().catch(reason => setError(String(reason)))}>放弃修改，重新读取</button></div>
      {preview && <div role="status"><p>排除 {preview.counts.skip || 0} 项 · 保留 {preview.counts.download || 0} 项 · 待复核 {preview.counts.review || 0} 项；下载意见变化 {preview.changed} 项。</p>
        {!!preview.examples.length && <details><summary>变化示例（最多20项）</summary>{preview.examples.map(item => <p key={item.item_id}>{item.title}：{item.before} → {item.after}</p>)}</details>}</div>}
      <details className="settings-advanced"><summary>规则版本记录与恢复</summary><select aria-label="历史规则版本" value={historyId} onChange={event => setHistoryId(event.target.value)}>
        <option value="">选择历史版本</option>{state?.history.map(plan => <option key={plan.id} value={plan.id} disabled={plan.active}>{plan.version}{plan.active ? "（当前）" : ""}</option>)}</select>
        <button disabled={busy || dirty || !historyId} onClick={async () => { setBusy(true); setError(""); try { await postApi(`/api/title-screening/${historyId}/activate`); await load(); setHistoryId(""); setMessage("历史版本已恢复并生效。") } catch(reason) {setError(String(reason))} finally {setBusy(false)} }}>恢复所选版本</button></details>
    </>}
    </fieldset>
  </div>
}
