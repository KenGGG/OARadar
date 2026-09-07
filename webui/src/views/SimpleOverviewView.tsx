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

function manifestProgressText(done: SimpleStatusResponse["done"], oa: SimpleStatusResponse["oa_activity"]): string | null {
  if (oa.status !== "working") return null
  const pending = done.waiting_download_items
  if (oa.progress_total == null || oa.progress_total <= 0) {
    return `已发现 ${done.oa_total.toLocaleString()} 项，正在扫描更多页面；待下载 ${pending.toLocaleString()} 项。`
  }
  return `已处理 ${(oa.progress_current || 0).toLocaleString()} / ${oa.progress_total.toLocaleString()} 项；待下载 ${pending.toLocaleString()} 项。`
}

function manifestDownloadCounters(done: SimpleStatusResponse["done"]) {
  return {
    discovered: done.oa_total,
    archiveComplete: done.archive_complete,
    pending: done.waiting_download_items,
    issues: done.download_issue_items,
    excluded: done.excluded,
  }
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
  const manifestProgress = manifestProgressText(done, oa)
  const manifestCounters = manifestDownloadCounters(done)
  const batch = data.local_delivery
  const total = batch?.scope_done_items || 0
  const processed = batch?.processed || 0
  const percent = total ? processed / total * 100 : 0

  return <section className="simple-overview">
    <div className={`simple-banner simple-banner-${banner.tone}`}>
      <ShieldCheck size={18}/>
      <span>{banner.text}</span>
      <small>数据更新于 {time(data.generated_at)}</small>
    </div>

    <article className="simple-card" aria-label="本地存量 Markdown 批量交付进度">
      <header><BookOpen size={18}/><strong>本地存量 Markdown · 整体进展</strong></header>
      <div className="simple-card-body">
        {!batch?.available ? <p>{batch?.message || '批量进度尚未取得'}</p> : <>
          <p className="simple-headline">已处理 {processed.toLocaleString()} / {total.toLocaleString()} 项（{percent.toFixed(1)}%）</p>
          <progress aria-label="存量事项处理进度" value={processed} max={total || 1} style={{width: '100%', height: 18}} />
          <div className="simple-metrics">
            {[
              ['完整交付', (batch.complete_new_or_updated || 0) + (batch.complete_reused || 0)],
              ['其中新增／更新', batch.complete_new_or_updated], ['其中有效复用', batch.complete_reused],
              ['部分交付', batch.partial], ['待复核', batch.final_needs_review],
              ['本轮已扫描排除', batch.excluded], ['失败／缺件', batch.failed_or_missing],
              ['待补证据', batch.awaiting_evidence], ['尚未处理', batch.not_processed],
            ].map(([label, value]) => <div className="simple-metric" key={label}><span>{label}</span><strong>{Number(value || 0).toLocaleString()}</strong></div>)}
          </div>
          <p className="simple-detail">附件 Markdown：{batch.attachment_markdown?.toLocaleString()} 个 · 事项索引：{batch.item_indexes?.toLocaleString()} 个</p>
          <p className="simple-detail">最近记录阶段：{({local_evidence_and_qwen: '本地证据与 Qwen 分类', processing: '事项处理', batch_finished: '当前批次结束'} as Record<string, string>)[batch.stage || ''] || '未知'}（阶段记录不代表进程存活）</p>
          <div className="simple-meta"><span>台账更新：{time(batch.updated_at || null)} · 页面每 5 秒刷新</span></div>
          {batch.stale && <p className="bad-text">台账已超过 15 分钟未更新，可能正在处理长任务或任务已停止。</p>}
          <p className="simple-detail">仅处理本地已有原件，不重新下载 OA。已处理包含排除、复核及失败；遍历完成不等于全部转换成功。</p>
        </>}
      </div>
    </article>

    <div className="simple-card-grid">
      {/* 已办知识库 */}
      <SimpleCard title="已办知识库" status={done.status} icon={BookOpen}>
        <p className="simple-headline">{done.headline}</p>
        <div className="simple-metrics">
          <div className="simple-metric"><span>已同步</span><strong>{num(done.oa_total, "尚未取得")}</strong></div>
          <div className="simple-metric"><span>原件完整</span><strong>{num(done.archive_complete, "尚未取得")}</strong></div>
          <div className="simple-metric"><span>MD 就绪</span><strong>{num(done.markdown_ready_items, "尚未取得")}</strong></div>
          <div className="simple-metric"><span>最终发布</span><strong>{num(done.published_items, "尚未取得")}</strong></div>
          <div className="simple-metric"><span>待下载</span><strong>{num(done.waiting_download_items, "尚未取得")}</strong></div>
          <div className="simple-metric"><span>待 MD 化</span><strong>{num(done.queued_items, "尚未取得")}</strong></div>
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
        <div className="simple-metrics">
          <div className="simple-metric"><span>已发现</span><strong>{num(manifestCounters.discovered, "尚未取得")}</strong></div>
          <div className="simple-metric"><span>原件完整</span><strong>{num(manifestCounters.archiveComplete, "尚未取得")}</strong></div>
          <div className="simple-metric"><span>待下载</span><strong>{num(manifestCounters.pending, "尚未取得")}</strong></div>
          <div className="simple-metric"><span>下载异常</span><strong className={manifestCounters.issues > 0 ? "bad-text" : ""}>{num(manifestCounters.issues, "尚未取得")}</strong></div>
          <div className="simple-metric"><span>已排除</span><strong>{num(manifestCounters.excluded, "尚未取得")}</strong></div>
        </div>
        {manifestProgress && <p className="simple-detail">{manifestProgress}</p>}
        {oa.progress_total != null && oa.progress_total > 0 && <div className="simple-meta"><span>进度 {oa.progress_current || 0} / {oa.progress_total}</span></div>}
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
