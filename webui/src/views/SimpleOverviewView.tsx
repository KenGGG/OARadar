import { Bell, BookOpen, CircleAlert, FileText, Server } from "lucide-react"
import { OverviewAuditPanel } from "./OverviewAuditPanel"
import type { BusinessTone, SimpleStatusResponse } from "../types/simple-status"
import { api, time } from "../App"

type Target = "overview" | "pending" | "done" | "markdown" | "settings"
const STATE: Record<BusinessTone, string> = { normal: "正常", completed: "当前任务已完成", attention: "需要处理", working: "处理中", fallback_used: "使用规则摘要", unknown: "状态未知", disabled: "未启用", not_run: "尚未运行" }
const tone = (status: BusinessTone) => status === "attention" ? "bad" : ["normal", "completed"].includes(status) ? "good" : ["working", "fallback_used"].includes(status) ? "warn" : "neutral"
export function SimpleOverviewView({ data, onJump }: { data: SimpleStatusResponse; onJump: (view: Target, filter?: string, item?: number | null) => void }) {
  const pending = data.pending, archive = data.archive, markdown = data.markdown
  const batch = data.local_delivery
  const total = batch?.scope_done_items || 0, processed = batch?.processed || 0
  const converted = markdown.successful_files || 0, expected = markdown.expected_files || 0
  const conversionState = { running: "正在转换", stopped: "转换任务已停止", blocked: "转换任务遇到问题", finished: "本轮处理已结束", unknown: "任务运行状态待核实" }
  const cards = [
    { title: "待办通知", icon: Bell, status: pending.status, headline: pending.headline, target: "pending" as Target,
      metrics: [["当前待办", pending.oa_pending_count], ["飞书成功", pending.feishu_sent], ["投递失败", pending.feishu_failed], ["投递待核对", pending.feishu_unknown], ["摘要失败", pending.model_failed], ["规则摘要", pending.model_fallback]],
      last: pending.last_feishu_success_at, next: pending.next_scan_at, scan: pending.last_scan_at },
    { title: "已办原件归档", icon: BookOpen, status: archive.status, headline: archive.headline, target: "done" as Target,
      metrics: [["已发现", archive.total], ["归档已完成", archive.complete], ["待下载", archive.pending], ["归档异常", archive.failed], ["已排除", archive.excluded]],
      last: archive.last_success_at, next: archive.next_run_at, scan: archive.last_scan_at },
    { title: "Markdown 交付", icon: FileText, status: markdown.status, headline: markdown.headline, target: "markdown" as Target,
      metrics: [["完整交付", markdown.complete], ["待处理", markdown.pending], ["部分交付", markdown.partial], ["交付失败", markdown.failed], ["待复核", markdown.review], ["已排除", markdown.excluded]],
      last: markdown.last_success_at, next: markdown.next_run_at, scan: markdown.last_scan_at },
  ]
  return <section className="simple-overview">
    <div className={`simple-banner simple-banner-${tone(data.overall_status)}`}><strong>{data.attention.length ? `有 ${data.attention.length} 类归档与交付问题待处理` : "归档与交付暂无已知异常"}</strong><small>数据更新于 {time(data.generated_at)}</small></div>
    <article className="simple-card overview-actions"><header><CircleAlert size={19}/><strong>需要你处理</strong><span className="overview-section-caption">优先处理阻塞，再确认差异</span></header><div className="simple-card-body">
      {data.oa_activity.status === "disconnected" && /失效|过期|失败|认证/.test(data.oa_activity.detail) && <div className="overview-login-alert"><strong>请检查 OA 登录</strong><p>前往已办资料，打开浏览器确认登录。密码仍由本机浏览器保存，登录后继续原有任务。</p><button className="simple-link" onClick={() => onJump("done")}>前往浏览器登录 →</button></div>}
      {data.attention.length ? <div className="overview-action-grid">{data.attention.map((item, index) => <button key={`${item.jump}-${item.filter}-${index}`} className={`overview-action ${item.severity}`} onClick={() => onJump(item.jump, item.filter)}><strong>{item.label}</strong><span>{item.filter === "needs_review" ? "确认事项来源与分类，必要时调整分类后重新交付。" : item.filter === "partial" ? "查看未成功转换的附件，补齐原件或重试失败部分。" : item.jump === "markdown" ? "检查失败原因，确认原件完整后重试转换。" : item.jump === "pending" ? "核对通知结果与配置，再决定是否重试，避免重复发送。" : "查看异常原因，确认登录和原件下载情况后再修复。"}</span><b>查看待处理事项 →</b></button>)}</div> : <p className="overview-empty">归档与交付暂无待处理提醒。在线附件核验的差异单独列在下方。</p>}
    </div></article>
    <OverviewAuditPanel onOpen={id => onJump("done", "", id)}/>
    <div className="overview-section-title"><h2>三条流程的当前进展</h2><p>原件归档、转换成功与完整交付分别统计。</p></div>
    <div className="simple-card-grid workflow-grid">{cards.map(card => <article className={`simple-card simple-card-${tone(card.status)}`} key={card.title}>
      <header><card.icon size={18}/><strong>{card.title}</strong><span className={`status status-${tone(card.status)}`}>{STATE[card.status]}</span></header>
      <div className="simple-card-body"><p className="simple-headline">{card.headline}</p><p className="simple-detail">最近成功：{time(card.last)}</p><p className="simple-detail">最近扫描：{time(card.scan)}</p><p className="simple-detail">{card.target === "markdown" ? "本地队列持续处理已验证原件" : `下次扫描：${time(card.next)}`}</p><div className="simple-metrics">{card.metrics.map(([label,value]) => <div className="simple-metric" key={label}><span>{label}</span><strong>{typeof value === "number" ? value.toLocaleString() : "—"}</strong></div>)}</div><button className="simple-link" onClick={() => onJump(card.target)}>查看{card.title} →</button></div>
    </article>)}</div>
    {batch?.live && <article className="simple-card batch-progress"><header><FileText size={18}/><strong>附件 Markdown 转换进度</strong><span>{batch.available ? conversionState[batch.status || "unknown"] : "状态暂不可读"}</span></header><div className="simple-card-body">
      <p>已成功转换 {converted.toLocaleString()} / {expected.toLocaleString()} 份附件{expected > 0 ? `（${(converted / expected * 100).toFixed(1)}%）` : ""} · 尚未成功 {Math.max(0, expected - converted).toLocaleString()} 份</p>
      <progress aria-label="附件转换成功进度" value={converted} max={expected || 1}/>
      {batch.stage === "preparing_markdown_delivery" ? <p>正在核对存量和分类结果：{processed} / {total} 项</p> : batch.current_title ? <p>当前事项：{batch.current_title}</p> : null}
      {batch.current_attachment && <p>当前附件：{batch.current_attachment}{batch.actual_format ? ` · ${batch.actual_format.toUpperCase()}` : ""}</p>}
      {batch.status === "stopped" || batch.status === "blocked" ? <p className="bad-text">任务未在正常运行，已转换成果保留。</p> : null}
      {batch.stale && batch.status === "running" && <p>当前步骤耗时较长，转换进程仍在运行。</p>}
      {!batch.available && <p>{batch.message}</p>}
      <small>任务更新：{time(batch.updated_at || null)} · 页面每 5 秒刷新；成功份数依据附件交付记录，事项遍历次数不代表转换成功。</small>
    </div></article>}
    {archive.daily_sync?.enabled && <article className="simple-card"><header><BookOpen size={18}/><strong>每日已办自动更新</strong><span>{archive.daily_sync.active ? "后台已启用" : "后台未运行"}</span></header><div className="simple-card-body"><p>自动同步完整已办清单，按筛选规则下载新增附件，再进入 Markdown 转换和来源分类。</p><p>下次同步：{time(archive.daily_sync.next_run_at || null)} · 最近完整同步：{time(archive.daily_sync.last_scan_at || null)}</p><p>本轮已有 {archive.daily_sync.local_tasks_enqueued || 0} 项进入转换分类队列，成功交付进度以附件 Markdown 记录为准。</p>{archive.daily_sync.last_error && <p className="bad-text">{archive.daily_sync.last_error}</p>}</div></article>}
    <details className="simple-card overview-issue-details"><summary><Server size={15}/> 浏览器会话与后台信息</summary><div className="simple-card-body"><p>{data.oa_activity.status === "disconnected" ? "交互浏览器当前未连接" : data.oa_activity.label}</p><p>{data.oa_activity.status === "disconnected" ? "在线核验、定时同步由各自任务执行，请以对应进度为准。需要手动检查登录时，可前往已办资料打开浏览器。" : data.oa_activity.detail}</p><small>最后心跳：{time(data.oa_activity.heartbeat_at)}</small>{data.oa_activity.status === "working" && <p>{data.oa_activity.progress_total ? `已处理 ${data.oa_activity.progress_current || 0} / ${data.oa_activity.progress_total} 项` : "正在扫描更多页面"}</p>}</div></details>
    {!batch?.live && <details className="simple-card batch-progress"><summary>本地存量批量交付进度</summary><div className="simple-card-body">{batch?.available ? <><p>已处理 {processed} / {total} 项</p><progress aria-label="存量事项处理进度" value={processed} max={total || 1}/><p>完整交付 {(batch.complete_new_or_updated || 0) + (batch.complete_reused || 0)} · 部分交付 {batch.partial || 0} · 失败／缺件 {batch.failed_or_missing || 0} · 待复核 {batch.final_needs_review || 0}</p><small>台账更新：{time(batch.updated_at || null)}；遍历完成不等于全部转换成功。</small>{batch.stale && <p className="bad-text">台账已超过 15 分钟未更新，请检查任务是否仍在运行。</p>}</> : <p>{batch?.message || "暂无批量进度记录"}</p>}</div></details>}
  </section>
}
export function loadSimpleStatus(): Promise<SimpleStatusResponse> { return api("/api/simple-status") }
