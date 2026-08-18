import { BookOpen, BrainCircuit, Clock, Server, ShieldCheck } from "lucide-react"
import type { BusinessTone, SimpleStatusResponse } from "../types/simple-status"
import { api, Badge, time } from "../App"

type Tone = "good" | "warn" | "bad" | "neutral"

function toneOf(status: BusinessTone): Tone {
  if (status === "completed" || status === "normal") return "good"
  if (status === "attention") return "bad"
  if (status === "working" || status === "fallback_used") return "warn"
  return "neutral"
}

function bannerText(data: SimpleStatusResponse): { text: string; tone: Tone } {
  if (data.overall_status === "attention") {
    return { text: `有 ${data.attention.length} 项需要处理。`, tone: "bad" }
  }
  if (data.done.status === "completed" && (data.pending.status === "normal" || data.pending.status === "fallback_used")) {
    return { text: "系统运行正常，两条业务链路均已完成当前任务。", tone: "good" }
  }
  return { text: "系统运行正常，但已办知识库仍在建设中。", tone: "warn" }
}

function num(value: number | null, missing: string): string {
  return value == null ? missing : value.toLocaleString()
}

function SimpleCard({ title, status, icon: Icon, children }: {
  title: string
  status: BusinessTone
  icon: React.ComponentType<{ size?: number }>
  children: React.ReactNode
}) {
  return <article className={`simple-card simple-card-${toneOf(status)}`}>
    <header><Icon size={18}/><strong>{title}</strong><Badge tone={toneOf(status)}>{status === "completed" || status === "normal" ? "正常" : status === "attention" ? "需要处理" : status === "fallback_used" ? "使用兜底" : "建设中"}</Badge></header>
    <div className="simple-card-body">{children}</div>
  </article>
}

export function SimpleOverviewView({ data, onJump }: {
  data: SimpleStatusResponse
  onJump: (view: "overview" | "done" | "settings") => void
}) {
  const banner = bannerText(data)
  const done = data.done
  const pending = data.pending
  const oa = data.oa_activity

  return <section className="simple-overview">
    <div className={`simple-banner simple-banner-${banner.tone}`}>
      <ShieldCheck size={18}/>
      <span>{banner.text}</span>
      <small>数据更新于 {time(data.generated_at)}</small>
    </div>

    <div className="simple-card-grid">
      {/* 已办知识库 */}
      <SimpleCard title="已办知识库" status={done.status} icon={BookOpen}>
        <p className="simple-headline">{done.headline}</p>
        <div className="simple-metrics">
          <div className="simple-metric"><span>已同步</span><strong>{num(done.oa_total, "尚未取得")}</strong></div>
          <div className="simple-metric"><span>原件完整</span><strong>{num(done.archive_complete, "尚未取得")}</strong></div>
          <div className="simple-metric"><span>MD 就绪</span><strong>{num(done.markdown_ready_items, "尚未取得")}</strong></div>
          <div className="simple-metric"><span>最终发布</span><strong>{num(done.published_items, "尚未取得")}</strong></div>
          <div className="simple-metric"><span>排队中</span><strong>{num(done.queued_items, "尚未取得")}</strong></div>
        </div>
        <div className="simple-meta"><span>最近扫描：{time(done.last_scan_at)}</span></div>
        <button className="simple-link" onClick={() => onJump("done")}>查看已办资料 →</button>
      </SimpleCard>

      {/* 待办飞书提醒 */}
      <SimpleCard title="待办飞书提醒" status={pending.status} icon={BrainCircuit}>
        <p className="simple-headline">{pending.headline}</p>
        <div className="simple-metrics">
          <div className="simple-metric"><span>当前待办</span><strong>{num(pending.oa_pending_count, "尚未取得")}</strong></div>
          <div className="simple-metric"><span>飞书成功</span><strong>{num(pending.feishu_sent, "尚未取得")}</strong></div>
          <div className="simple-metric"><span>飞书失败</span><strong className={pending.feishu_failed > 0 ? "bad-text" : ""}>{num(pending.feishu_failed, "0")}</strong></div>
          <div className="simple-metric"><span>模型成功</span><strong>{num(pending.model_success, "尚未取得")}</strong></div>
          <div className="simple-metric"><span>模型兜底</span><strong>{num(pending.model_fallback, "0")}</strong></div>
          <div className="simple-metric"><span>模型失败</span><strong className={pending.model_failed > 0 ? "bad-text" : ""}>{num(pending.model_failed, "0")}</strong></div>
        </div>
        <div className="simple-meta">
          <span><Clock size={13}/>{pending.frequency_text}</span>
          <span>最近扫描：{time(pending.last_scan_at)}</span>
          <span>下次扫描：{time(pending.next_scan_at)}</span>
        </div>
        <div className="simple-meta"><span>当前模型：{pending.model_name || "尚未取得"}</span></div>
      </SimpleCard>

      {/* OA 后台状态 */}
      <SimpleCard title="OA 后台状态" status={oa.status === "unknown" ? "unknown" : oa.status === "disconnected" || oa.status === "logging_in" ? "working" : oa.status === "working" ? "working" : "normal"} icon={Server}>
        <p className="simple-headline">{oa.label}</p>
        <p className="simple-detail">{oa.detail}</p>
        {oa.progress_total != null && <div className="simple-meta"><span>进度 {oa.progress_current || 0} / {oa.progress_total}</span></div>}
        <div className="simple-meta"><span>最后心跳：{time(oa.heartbeat_at)}</span></div>
      </SimpleCard>
    </div>

    <div className="section-toolbar"><div><h2>需要人工处理</h2><p>仅列出真正需要干预的问题，点击跳转到对应入口。</p></div></div>
    {data.attention.length
      ? <div className="attention-list">{data.attention.map((item, index) => (
          <button key={index} className={`attention-item ${item.severity}`} onClick={() => onJump(item.jump)}>
            <span className="label">{item.label}</span>
            <span className="attention-go">去处理 →</span>
          </button>
        ))}</div>
      : <div className="empty panel">当前没有需要人工处理的问题。</div>}
  </section>
}

export function loadSimpleStatus(): Promise<SimpleStatusResponse> {
  return api<SimpleStatusResponse>("/api/simple-status")
}
