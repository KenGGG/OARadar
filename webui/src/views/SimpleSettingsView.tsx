import { useEffect, useState } from "react"
import {
  Archive, Bell, BookOpen, BrainCircuit, Save, Server, ShieldCheck,
} from "lucide-react"
import type { SettingsData, ScheduleData, ServiceStatus } from "../App"
import {
  api, postApi, csrf, time, Badge, Field, NumberField, Toggle, SecretState, ServiceCard, SERVICE_TITLES,
} from "../App"

export function SimpleSettingsView({ initial, onJumpAdvanced }: {
  initial: SettingsData
  onJumpAdvanced: () => void
}) {
  const [form, setForm] = useState<SettingsData>(initial)
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState("")
  const [schedule, setSchedule] = useState<ScheduleData | null>(null)

  useEffect(() => { setForm(initial) }, [initial])
  useEffect(() => {
    void (async () => { try { setSchedule(await api<ScheduleData>("/api/schedule/status")) } catch { /* ignore */ } })()
  }, [])

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

  const services = schedule ? (Object.entries(schedule.services) as [keyof ScheduleData["services"], ServiceStatus][]) : []

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

    {/* 默认服务状态摘要（spec §6.3）：五个服务是否运行 + 扫描频率 */}
    <div className="settings-panel"><h2><Server size={18}/>本地服务</h2>
      <p className="settings-note">线上核验、Source Markdown 明细、人工复核、数据治理与维护操作均收进下方高级维护。</p>
      {schedule
        ? <>
          <div className="service-grid-5">{services.map(([key, svc]) => <ServiceCard key={key} title={SERVICE_TITLES[key] || key} svc={svc}/>)}</div>
          <div className="section-toolbar"><div><h3>扫描计划</h3><p>待办每小时 05 分检查；已办夜间全量扫描每日 23:30。</p></div></div>
          <div className="detail-grid">
            <div className="info"><span>每小时定时</span><strong>{schedule.hourly_enabled ? "已启用" : "未启用"}</strong></div>
            <div className="info"><span>最近扫描</span><strong>{time(schedule.last_scan_at)}</strong></div>
            <div className="info"><span>下次执行</span><strong>{time(schedule.next_run_at)}</strong></div>
            <div className="info"><span>系统调度可用</span><strong>{schedule.schedule_available ? "是" : "否"}</strong></div>
          </div>
          {schedule.summary.nightly.last_at && <div className="detail-grid">
            <div className="info"><span>最近夜间全量</span><strong>{time(schedule.summary.nightly.last_at)}</strong></div>
            <div className="info"><span>线上事项</span><strong>{schedule.summary.nightly.source_total.toLocaleString()}</strong></div>
            <div className="info"><span>新增</span><strong>{schedule.summary.nightly.new_items.toLocaleString()}</strong></div>
            <div className="info"><span>内容变化</span><strong>{schedule.summary.nightly.changed_items.toLocaleString()}</strong></div>
          </div>}
        </>
        : <div className="empty panel">服务状态加载中…</div>}
      <div className="toolbar-actions">
        <button onClick={onJumpAdvanced}><ShieldCheck size={16}/>展开高级维护</button>
      </div>
    </div>

    <div className="settings-head">
      <button className="button-primary" onClick={() => void save()} disabled={saving}><Save size={16}/>{saving ? "保存中" : "保存设置"}</button>
    </div>
    {message && <div className="settings-message">{message}</div>}
  </section>
}
