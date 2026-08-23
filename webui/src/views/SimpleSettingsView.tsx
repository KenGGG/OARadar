import { useEffect, useState } from "react"
import {
  Archive, Bell, BookOpen, BrainCircuit, Save, Trash2,
} from "lucide-react"
import type { SettingsData } from "../App"
import {
  api, postApi, deleteApi, Field, NumberField, Toggle, SecretState,
} from "../App"

type TitleExclusionPolicy = { id: number; pattern: string; action: string; scope: string; enabled: boolean }

export function SimpleSettingsView({ initial }: {
  initial: SettingsData
}) {
  const [form, setForm] = useState<SettingsData>(initial)
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState("")
  const [titleKeywords, setTitleKeywords] = useState("")
  const [policies, setPolicies] = useState<TitleExclusionPolicy[]>([])

  useEffect(() => { setForm(initial) }, [initial])
  const loadPolicies = async () => {
    const result = await api<TitleExclusionPolicy[]>("/api/policies")
    setPolicies(result.filter(policy => policy.scope === "title" && policy.action === "skip" && policy.enabled))
  }
  useEffect(() => { void loadPolicies() }, [])

  const saveTitleKeywords = async () => {
    if (!titleKeywords.trim()) return
    setSaving(true); setMessage("")
    try {
      const result = await postApi<{ keyword_count: number; applied_count: number }>("/api/policies/bulk", {
        text: titleKeywords, action: "skip", scope: "title",
      })
      setTitleKeywords("")
      await loadPolicies()
      setMessage(`已保存 ${result.keyword_count} 个标题排除关键词；后续已办同步会在进入详情页前应用。`)
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : "保存失败") }
    finally { setSaving(false) }
  }

  const removeTitleKeyword = async (policy: TitleExclusionPolicy) => {
    setSaving(true); setMessage("")
    try {
      const result = await deleteApi<{ affected_count: number }>(`/api/policies/${policy.id}`)
      await loadPolicies()
      setMessage(`已删除“${policy.pattern}”；已重新判定 ${result.affected_count} 个事项。`)
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : "删除失败") }
    finally { setSaving(false) }
  }

  const save = async () => {
    setSaving(true); setMessage("")
    const payload = {
      llm: {
        enabled: form.summary_model.enabled, active_provider: form.summary_model.active_provider,
        ollama_base_url: form.summary_model.ollama_base_url, ollama_model: form.summary_model.ollama_model,
        timeout_seconds: Number(form.summary_model.timeout_seconds), max_tokens: Number(form.summary_model.max_tokens),
        temperature: Number(form.summary_model.temperature), max_retries: Number(form.summary_model.max_retries),
        max_concurrency: Number(form.summary_model.max_concurrency),
      },
      feishu: {
        enabled: form.feishu.enabled, message_type: form.feishu.message_type,
        max_items_per_section: Number(form.feishu.max_items_per_section), redact_confidential: form.feishu.redact_confidential,
        retry_attempts: Number(form.feishu.retry_attempts),
      },
      pending_cleanup: { ...form.data_cleanup },
      markdown_export: { ...form.markdown },
    }
    try {
      await postApi("/api/settings", payload)
      setMessage("设置已保存，需要重启服务后生效")
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : "保存失败") }
    finally { setSaving(false) }
  }

  const toggle = (group: "summary_model" | "feishu" | "data_cleanup" | "markdown", key: string, value: boolean | string | number) =>
    setForm(f => ({ ...f, [group]: { ...(f[group] as Record<string, unknown>), [key]: value } }))

  return <section className="settings-stack">
    <div className="settings-panel"><h2><Bell size={18}/>待办监控与飞书</h2>
      <div className="settings-sub"><h3>扫描计划</h3>
        <Toggle label="启用待办监控（飞书）" checked={form.feishu.enabled} change={v => toggle("feishu", "enabled", v)}/>
        <Toggle label="启用智能摘要" checked={form.summary_model.enabled} change={v => toggle("summary_model", "enabled", v)}/>
      </div>
      <div className="settings-sub"><h3>摘要模型</h3>
        <div className="field-pair">
          <Field label="API 地址" value={form.summary_model.ollama_base_url} change={v => toggle("summary_model", "ollama_base_url", v)}/>
          <Field label="模型" value={form.summary_model.ollama_model} change={v => toggle("summary_model", "ollama_model", v)}/>
        </div>
        <div className="field-pair">
          <NumberField label="超时（秒）" value={form.summary_model.timeout_seconds} change={v => toggle("summary_model", "timeout_seconds", v)}/>
          <NumberField label="最大输出" value={form.summary_model.max_tokens} change={v => toggle("summary_model", "max_tokens", v)}/>
          <NumberField label="温度" value={form.summary_model.temperature} step="0.1" change={v => toggle("summary_model", "temperature", v)}/>
          <NumberField label="最大并发" value={form.summary_model.max_concurrency} change={v => toggle("summary_model", "max_concurrency", v)}/>
        </div>
        <SecretState label={form.feishu.webhook_env || "FEISHU_WEBHOOK"} configured={!!form.feishu.webhook_configured}/>
        <SecretState label={form.feishu.secret_env || "FEISHU_SECRET"} configured={!!form.feishu.secret_configured}/>
      </div>
      <div className="settings-sub"><h3>飞书通知</h3>
        <div className="field-pair">
          <NumberField label="单次最大事项数" value={form.feishu.max_items_per_section} change={v => toggle("feishu", "max_items_per_section", v)}/>
          <NumberField label="重试次数" value={form.feishu.retry_attempts} change={v => toggle("feishu", "retry_attempts", v)}/>
        </div>
        <Toggle label="通知内容脱敏" checked={form.feishu.redact_confidential} change={v => toggle("feishu", "redact_confidential", v)}/>
      </div>
      <div className="settings-sub"><h3>数据清理</h3>
        <Toggle label="飞书成功后自动清理" checked={form.data_cleanup.auto_cleanup_after_success} change={v => toggle("data_cleanup", "auto_cleanup_after_success", v)}/>
        <div className="field-pair">
          <NumberField label="清理延迟（小时）" value={form.data_cleanup.cleanup_delay_hours} change={v => toggle("data_cleanup", "cleanup_delay_hours", v)}/>
          <NumberField label="失败数据保留天数" value={form.data_cleanup.failed_retention_days} change={v => toggle("data_cleanup", "failed_retention_days", v)}/>
        </div>
        <Toggle label="保留摘要正文" checked={form.data_cleanup.keep_summary_body} change={v => toggle("data_cleanup", "keep_summary_body", v)}/>
        <Toggle label="保留页面快照" checked={form.data_cleanup.keep_page_snapshot} change={v => toggle("data_cleanup", "keep_page_snapshot", v)}/>
        <Toggle label="保留临时附件" checked={form.data_cleanup.keep_temp_attachments} change={v => toggle("data_cleanup", "keep_temp_attachments", v)}/>
      </div>
    </div>

    <div className="settings-panel"><h2><Archive size={18}/>已办归档</h2>
      <div className="detail-grid">
        <div className="info"><span>已办监控</span><strong>{initial.done_archive.enabled ? "已启用" : "未启用"}</strong></div>
        <div className="info"><span>永久归档根目录</span><strong>{initial.done_archive.archive_dir}</strong></div>
        <div className="info"><span>计算 SHA256</span><strong>{initial.done_archive.compute_sha256 ? "是" : "否"}</strong></div>
        <div className="info"><span>压缩包展开深度</span><strong>{String(initial.done_archive.max_attachment_depth)}</strong></div>
      </div>
      <div className="settings-sub"><h3>标题排除关键词</h3>
        <p className="settings-help">命中关键词的已办事项不会进入详情页，也不会下载附件。每行一个关键词。</p>
        <textarea className="settings-textarea" value={titleKeywords} onChange={e => setTitleKeywords(e.target.value)} placeholder="例如：会议通知" />
        <div className="policy-actions"><button className="button-primary" onClick={() => void saveTitleKeywords()} disabled={saving || !titleKeywords.trim()}><Save size={16}/>保存关键词</button></div>
        <div className="policy-list">
          {policies.map(policy => <div className="policy-row" key={policy.id}><span>{policy.pattern}</span><button className="icon-button" title={`删除 ${policy.pattern}`} onClick={() => void removeTitleKeyword(policy)} disabled={saving}><Trash2 size={15}/></button></div>)}
          {!policies.length && <span className="settings-help">尚未设置标题排除关键词。</span>}
        </div>
      </div>
    </div>

    <div className="settings-panel"><h2><BookOpen size={18}/>Source Markdown 生成</h2>
      <Toggle label="启用 Source Markdown 生成" checked={form.markdown.enabled} change={v => toggle("markdown", "enabled", v)}/>
      <Field label="中间输出目录" value={String(form.markdown.source_markdown_dir ?? "")} change={v => toggle("markdown", "source_markdown_dir", v)}/>
      <Field label="Workspace 根目录" value={String(form.markdown.workspace_root ?? "")} change={v => toggle("markdown", "workspace_root", v)}/>
      <Toggle label="保持来源目录结构" checked={!!form.markdown.preserve_source_tree} change={v => toggle("markdown", "preserve_source_tree", v)}/>
      <Toggle label="写入 YAML frontmatter" checked={!!form.markdown.write_frontmatter} change={v => toggle("markdown", "write_frontmatter", v)}/>
      <Toggle label="原子发布" checked={!!form.markdown.atomic_publish} change={v => toggle("markdown", "atomic_publish", v)}/>
    </div>

    <div className="settings-panel"><h2><BrainCircuit size={18}/>知识归档发布</h2>
      <div className="detail-grid">
        <div className="info"><span>知识工作区根目录</span><strong>{initial.llm_wiki.workspace_root}</strong></div>
        <div className="info"><span>Source Markdown 目录</span><strong>{initial.llm_wiki.source_dir}</strong></div>
        <div className="info"><span>发布目录存在</span><strong>{initial.llm_wiki.source_dir_exists ? "是" : "否"}</strong></div>
        <div className="info"><span>发布目录可写</span><strong>{initial.llm_wiki.source_dir_writable ? "是" : "否"}</strong></div>
        <div className="info"><span>写入元数据头</span><strong>{initial.llm_wiki.write_frontmatter ? "是" : "否"}</strong></div>
        <div className="info"><span>原子发布</span><strong>{initial.llm_wiki.atomic_publish ? "是" : "否"}</strong></div>
      </div>
    </div>

    <div className="settings-head">
      <button className="button-primary" onClick={() => void save()} disabled={saving}><Save size={16}/>{saving ? "保存中" : "保存设置"}</button>
    </div>
    {message && <div className="settings-message">{message}</div>}
  </section>
}
