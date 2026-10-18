import { useEffect, useState } from "react"
import { api, postApi } from "../App"

type Mapping = { id: string; name: string; issuer_name_pattern?: string; document_code_pattern?: string; title_pattern?: string }
type Condition = { op: string; field?: string; value?: string; conditions?: Condition[]; condition?: Condition }
type Supplement = { id: string; name: string; kind: string; category: string; enabled: boolean; label: string; reason: string; condition: Condition }
type Spec = { version: string; second_pass?: { enabled: boolean; rules: Supplement[] }; classification: { self_workflow_title_patterns: string[] }; mappings: { self_organization: Mapping; group_organizations: Mapping[]; external_organizations: Mapping[]; publication_series: Mapping[] } }
type State = { plan_id: number | null; spec: Spec | null; history: { id: number; version: string; active: boolean }[] }
const labels: Record<string, string> = { SELF: "本公司资料", GROUP: "集团及成员企业", EXTERNAL: "外部机构来文", PUBLICATION: "刊物与学习资料", UNKNOWN: "待确认", EXCLUDED: "排除，仅索引", MANUAL: "历史人工分类" }

export function ArchiveClassificationSettings({ onDirtyChange }: { onDirtyChange: (value: boolean) => void }) {
  const [state, setState] = useState<State | null>(null)
  const [draft, setDraft] = useState<Spec | null>(null)
  const [baseline, setBaseline] = useState("")
  const [preview, setPreview] = useState<Record<string, number> | null>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState("")
  const [error, setError] = useState("")
  const dirty = !!draft && JSON.stringify(draft) !== baseline
  useEffect(() => onDirtyChange(dirty), [dirty, onDirtyChange])
  const load = async () => { const result = await api<State>("/api/archive-classification/settings"); setState(result); setDraft(result.spec); setBaseline(JSON.stringify(result.spec)); setPreview(null) }
  useEffect(() => { void load().catch(e => setError(String(e))) }, [])
  const change = (next: Spec) => { setDraft(next); setPreview(null); setMessage("") }
  const run = async (operation: "preview" | "save") => {
    setBusy(true); setError("")
    try { const result = await postApi<{counts: Record<string, number>}>(`/api/archive-classification/settings/${operation}`, {base_plan_id: state?.plan_id, spec: draft}); if (operation === "save") { await load(); setMessage("分类新版本已启用，下载筛选、原件位置和人工意见保留。") } else setPreview(result.counts) }
    catch (e) { setError(String(e)) } finally { setBusy(false) }
  }
  const editor = (record: Mapping, update: (value: Mapping) => void) => <details key={record.id}><summary>{record.name} · {record.id}</summary>
    {(["name", ...(record.title_pattern !== undefined ? ["title_pattern"] : ["issuer_name_pattern", "document_code_pattern"])] as (keyof Mapping)[]).map(key => <label key={key}>{({name: "目录名称", issuer_name_pattern: "发文机构表达式", document_code_pattern: "正式文号前缀表达式", title_pattern: "刊物标题表达式"} as Record<string, string>)[key]}<input value={record[key] || ""} onChange={e => update({...record, [key]: e.target.value})}/></label>)}
  </details>
  const conditionEditor = (node: Condition, update: (value: Condition) => void): React.ReactNode => <div>
    {node.op === "regex" && <label>匹配表达式（{node.field}）<textarea className="settings-textarea" value={node.value || ""} onChange={event => update({...node, value: event.target.value})}/></label>}
    {node.conditions?.map((child, index) => <div key={index}>{conditionEditor(child, value => update({...node, conditions: node.conditions!.map((c, i) => i === index ? value : c)}))}</div>)}
    {node.condition && conditionEditor(node.condition, value => update({...node, condition: value}))}
  </div>
  return <div className="settings-sub settings-sub-card"><h3>五类归档分类规则</h3>
    <p className="settings-help">只分类保留下载的事项。目录按机构或刊物系列、年份和完整 OA ID 组织；不创建月份、项目或部门目录。来源不明进入待确认。这里可微调机构、文号与刊物表达式。</p>
    {error && <p role="alert">{error}</p>}{message && <p role="status">{message}</p>}
    {draft && <fieldset disabled={busy} style={{border: 0, padding: 0, minWidth: 0}}><p>当前版本：{state?.spec?.version}</p>
      <details><summary>本公司业务流程标题规则</summary><textarea className="settings-textarea" aria-label="本公司业务流程标题规则" value={draft.classification.self_workflow_title_patterns.join("\n")} onChange={e => change({...draft, classification: {...draft.classification, self_workflow_title_patterns: e.target.value.split("\n").filter(Boolean)}})}/></details>
      {editor(draft.mappings.self_organization, value => change({...draft, mappings: {...draft.mappings, self_organization: value}}))}
      {(["group_organizations", "external_organizations", "publication_series"] as const).map(key => <details key={key}><summary>{({group_organizations: "集团及成员企业", external_organizations: "外部机构", publication_series: "刊物系列"})[key]}（{draft.mappings[key].length}）</summary>{draft.mappings[key].map((record, index) => editor(record, value => change({...draft, mappings: {...draft.mappings, [key]: draft.mappings[key].map((r, i) => i === index ? value : r)}})))}</details>)}
      {draft.second_pass && <details><summary>第二轮补充规律（{draft.second_pass.rules.length} 条）</summary>
        <p className="settings-help">仅匹配基础规则仍待确认的保留下载事项。主题归集不会确认来源机构；人工重点审核保持待确认。转发的外部制度不会因提到业务审批而归为本公司。</p>
        <label><input type="checkbox" checked={draft.second_pass.enabled} onChange={e => change({...draft, second_pass: {...draft.second_pass!, enabled: e.target.checked}})}/>启用第二轮补充</label>
        {draft.second_pass.rules.map((rule, index) => {
          const update = (value: Supplement) => change({...draft, second_pass: {...draft.second_pass!, rules: draft.second_pass!.rules.map((r, i) => i === index ? value : r)}})
          return <details key={rule.id}><summary>{rule.name} · {rule.id} · {rule.enabled ? "启用" : "停用"}</summary>
            <label><input type="checkbox" checked={rule.enabled} onChange={e => update({...rule, enabled: e.target.checked})}/>启用此补充规则</label>
            <p>{rule.reason} · {labels[rule.category]} · {rule.kind === "theme" ? "按主题归集，来源待核实" : rule.kind === "review" ? "仅人工审核建议" : "标题规律分类"}</p>
            {rule.label && <label>归集目录名称<input value={rule.label} onChange={e => update({...rule, label: e.target.value})}/></label>}
            {conditionEditor(rule.condition, value => update({...rule, condition: value}))}
          </details>
        })}
      </details>}
      {preview && <p role="status">预览：{Object.entries(preview).map(([key, count]) => `${labels[key] || key} ${count}`).join("；")}。下载决定保持不变。</p>}
      <div className="screening-toolbar"><button type="button" onClick={() => void run("preview")}>预览分类影响</button><button type="button" disabled={!dirty || !preview} onClick={() => void run("save")}>保存并启用分类新版本</button><button type="button" onClick={() => void load().catch(e => setError(String(e)))}>恢复当前分类设置</button></div>
      <details><summary>分类版本历史</summary>{state?.history.map(plan => <p key={plan.id}>{plan.version} {plan.active ? "（当前）" : <button type="button" onClick={async () => {setBusy(true); try {await postApi(`/api/archive-classification/${plan.id}/activate`); await load()} catch (e) {setError(String(e))} finally {setBusy(false)}}}>启用此版本</button>}</p>)}</details>
    </fieldset>}
  </div>
}
