import { useEffect, useState } from "react"
import { ArrowRight, CircleAlert, ShieldCheck } from "lucide-react"
import { api, postApi, time } from "../App"

type Filter = "review" | "access_failed" | "missing_download" | "historical_retained"
type Audit = {
  run: null | { id: number; status: string; total_items: number; completed_items: number; matched_items: number; verification_mode: string; started_at: string | null }
  status_counts?: Record<string, number>; excluded_items?: number
  items: { id: number; archive_item_id?: number | null; title: string; status: string; recognized_attachments: number | null; downloaded_attachments: number }[]
  item_pagination: { pages: number; total: number }
}
const STATES: Record<string, string> = { queued: "等待核验", running: "逐项核验中", completed: "本轮核验已结束", paused: "核验已暂停", pause_requested: "正在暂停", failed: "核验中断" }
const FILTERS: { key: Filter; label: string; guidance: string }[] = [
  { key: "review", label: "需人工确认", guidance: "打开对应事项，核对附件名称与数量。数量相同但标识变化，也需要确认；暂不据此删除本地文件。" },
  { key: "access_failed", label: "访问失败", guidance: "先确认浏览器仍能登录 OA，再打开失败事项检查访问权限和页面是否正常。访问失败不能认定为没有附件。" },
  { key: "missing_download", label: "待补下载", guidance: "系统已发现缺失原件并安排补下载。查看归档详情确认结果；仍未补齐时，再尝试从 OA 手动下载。" },
  { key: "historical_retained", label: "历史保留", guidance: "本地文件数多于线上当前附件数，可能是历史版本。请先核实来源，保留文件直至确认可以清理。" },
]
export function OverviewAuditPanel({ onOpen }: { onOpen: (id: number | null) => void }) {
  const [data, setData] = useState<Audit | null>(null)
  const [filter, setFilter] = useState<Filter>("review")
  const [page, setPage] = useState(1)
  const [error, setError] = useState("")
  const [updated, setUpdated] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [revision, setRevision] = useState(0)
  useEffect(() => {
    let alive = true, timer: ReturnType<typeof setTimeout>
    const load = async () => {
      try {
        const result = await api<Audit>(`/api/overview/online-audit?item_filter=${filter}&item_page=${page}&item_page_size=10`)
        if (alive) { setData(result); setError(""); setUpdated(new Date().toISOString()) }
      } catch { if (alive) setError("在线核验状态暂时无法读取，请稍后刷新。") }
      finally { if (alive) timer = setTimeout(load, 10000) }
    }
    setData(null)
    void load()
    return () => { alive = false; clearTimeout(timer) }
  }, [filter, page, revision])
  const run = data?.run
  const counts = data?.status_counts || {}
  const review = Math.max(0, ["inventory_mismatch", "content_mismatch", "content_unverified", "depth_limit_reached"].reduce((sum, key) => sum + (counts[key] || 0), 0) - (data?.excluded_items || 0))
  const count = (key: Filter) => key === "review" ? review : counts[key] || 0
  const percent = run?.total_items ? Math.min(100, run.completed_items / run.total_items * 100) : 0
  const resume = async () => {
    if (!run) return
    setBusy(true)
    try { await postApi(`/api/overview/online-audit/${run.id}/resume`); setRevision(value => value + 1) }
    catch { setError("继续核验失败，请检查后台状态后重试。") }
    finally { setBusy(false) }
  }
  return <article className="simple-card overview-audit">
    <header><ShieldCheck size={19}/><strong>在线附件核验与修复</strong><span className={`status status-${run?.status === "failed" || run?.status === "paused" ? "bad" : "neutral"}`}>{run ? STATES[run.status] || "状态待确认" : data ? "尚未开始核验" : "正在读取"}</span></header>
    <div className="simple-card-body">
      {error && <p className="overview-inline-error" role="status"><CircleAlert size={16}/>{error}</p>}
      {run ? <>
        <div className="overview-audit-progress"><div><strong>{run.completed_items.toLocaleString()} <span>/ {run.total_items.toLocaleString()} 项</span></strong><span>{percent.toFixed(1)}%</span></div><progress aria-label="在线逐项核验进度" value={run.completed_items} max={run.total_items || 1}/></div>
        <p className="simple-detail">已核验一致 {run.matched_items.toLocaleString()} 项 · 按规则排除 {data?.excluded_items || 0} 项 · 剩余 {Math.max(0, run.total_items - run.completed_items).toLocaleString()} 项</p>
        <p className="overview-audit-note">{run.verification_mode === "inventory" ? "逐个打开 OA 详情，核对附件数量与标识；本轮未重新比对文件内容。" : "逐个读取 OA 附件，比对附件清单与文件内容。"}{run.status !== "completed" ? "结果随核验推进更新。" : "核验结束不代表差异已修复或补下载已完成。"}</p>
        {run.status === "failed" && <p className="overview-inline-error" role="status">核验任务已中断，已完成的结果保留。可继续已有核验；若再次中断，需要修复后台异常。</p>}
        {["paused", "failed"].includes(run.status) && <button className="button-primary" disabled={busy} onClick={() => void resume()}>{busy ? "正在继续…" : "继续已有核验"}</button>}
        <div className="overview-audit-tabs" role="group" aria-label="核验问题分类">{FILTERS.map(item => <button key={item.key} aria-pressed={filter === item.key} onClick={() => { setFilter(item.key); setPage(1) }}><span>{item.label}</span><strong>{count(item.key).toLocaleString()}</strong></button>)}</div>
        <p className="overview-guidance">{FILTERS.find(item => item.key === filter)?.guidance}</p>
        {data?.items.length ? <details className="overview-issue-details" key={filter}><summary>查看 {data.item_pagination.total} 项具体事项</summary><div className="overview-issue-list">{data.items.map(item => <div className="overview-issue" key={item.id}><div><strong>{item.title || "未命名事项"}</strong><span>线上附件 {item.recognized_attachments ?? "未读取"} · 本地原件 {item.downloaded_attachments}</span></div><button className="simple-link" onClick={() => onOpen(item.archive_item_id ?? null)}>{item.archive_item_id ? "查看事项" : "查看已办"}<ArrowRight size={14}/></button></div>)}</div>
          {data.item_pagination.pages > 1 && <div className="pagination overview-pagination"><span>共 {data.item_pagination.total} 项 · 第 {page} / {data.item_pagination.pages} 页</span><button disabled={page <= 1} onClick={() => setPage(value => value - 1)}>上一页</button><button disabled={page >= data.item_pagination.pages} onClick={() => setPage(value => value + 1)}>下一页</button></div>}
        </details> : <p className="overview-empty">当前已核验范围内暂无此类事项。</p>}
      </> : data && <p className="overview-empty">暂无在线核验记录。归档与交付异常仍会显示在下方处理清单。</p>}
      {updated && <p className="overview-updated-at">状态更新：{time(updated)} · 每 10 秒自动读取</p>}
    </div>
  </article>
}
