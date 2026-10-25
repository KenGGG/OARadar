import { useEffect, useState } from "react"
import { api, postApi } from "../App"

type Job = { id: number; status: string; current: number; total: number | null;
  pause_requested: boolean; last_error: string | null; active_item: string | null; failed: number; deferred?: number }
type State = { inventory: Job | null; download: Job | null; login: Job | null; review: Job | null;
  inventory_complete: boolean; connection_configured: boolean; credential_storage: string;
  suggestions: { keyword: string; count: number; examples: string[] }[] }
const LABELS: Record<string, string> = { queued: "等待执行", running: "执行中", paused: "已暂停",
  auth_required: "需要登录", completed: "已完成", failed: "需要重试" }
const RESUMABLE = ["paused", "auth_required", "failed"]

export function DoneCollectionPanel({ refresh }: { refresh: () => Promise<void> }) {
  const [state, setState] = useState<State | null>(null)
  const [error, setError] = useState("")
  const [message, setMessage] = useState("")
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    let live = true
    const poll = async () => {
      try { const result = await api<State>("/api/done-collection"); if (live) setState(result) }
      catch (reason) { if (live) setError(String(reason)) }
    }
    void poll()
    const timer = window.setInterval(() => { void poll() }, 3000)
    return () => { live = false; window.clearInterval(timer) }
  }, [])
  const action = async (path: string, body?: object, success?: string) => {
    setBusy(true); setError(""); setMessage("")
    try { await postApi(path, body); if (success) setMessage(success); setState(await api<State>("/api/done-collection")); await refresh() }
    catch (reason) { setError(reason instanceof Error ? reason.message : "操作失败") }
    finally { setBusy(false) }
  }
  const running = [state?.inventory, state?.download, state?.login, state?.review].some(job => job && ["running", "queued"].includes(job.status))
  const runJob = (job: Job | null, operation: string) => job && RESUMABLE.includes(job.status)
    ? action(`/api/done-collection/${job.id}/resume`) : action("/api/done-collection/start", { operation })
  const taskRow = (job: Job | null, title: string, operation: string, blocked = false) => {
    const active = !!job && ["running", "queued"].includes(job.status)
    const percent = job?.total ? Math.min(100, Math.round(job.current / job.total * 100)) : 0
    return <div className="collection-task">
      <div className="collection-task-heading"><strong>{title}</strong><span>{job ? LABELS[job.status] : "尚未开始"}</span></div>
      <div className="collection-task-progress">
        {job?.total != null ? <><progress aria-label={`${title}进度`} value={job.current} max={job.total || 1}/><span>{job.current.toLocaleString()} / {job.total.toLocaleString()} 项 · {percent}%</span></>
          : <span className="collection-note">{operation === "done_inventory" ? "自动读取 OA 当前总数" : "同步清单后下载未完成原件"}</span>}
      </div>
      {active ? <button disabled={busy || job!.pause_requested} onClick={() => void action(`/api/done-collection/${job!.id}/pause`)}>{job!.pause_requested ? "等待暂停…" : "暂停"}</button>
        : <button disabled={busy || running || blocked || !state?.connection_configured} onClick={() => void runJob(job, operation)}>{job && RESUMABLE.includes(job.status) ? "继续 / 重试" : operation === "done_inventory" ? job ? "更新清单" : "同步清单" : "下载原件"}</button>}
      {job?.last_error && <p className="collection-task-detail">{job.last_error}</p>}
      {job?.active_item && active && <p className="collection-task-detail">正在下载：{job.active_item}</p>}
      {!!job?.deferred && <p className="collection-task-detail">{job.deferred} 项下载超时或卡住，已暂缓；{active ? "其余事项继续下载，本轮结束后可重试。" : "点击继续 / 重试可重新尝试。"}</p>}
      {!!job?.failed && job.failed > (job.deferred || 0) && <p className="collection-task-detail">另有 {job.failed - (job.deferred || 0)} 项下载失败，可继续重试。</p>}
    </div>
  }
  return <section className="collection-panel">
    <div className="collection-title"><h2>全量采集</h2><span className="collection-note">先同步清单，再下载原件；中断后可继续。</span></div>
    {state && !state.connection_configured && <p className="collection-note" role="status">请先填写项目根目录的 .env 中的 OA_BASE_URL，保存后刷新页面。</p>}
    {error && <p className="collection-error" role="alert">{error}</p>}
    {message && <p role="status">{message}</p>}
    {taskRow(state?.inventory || null, "清单同步", "done_inventory")}
    {taskRow(state?.download || null, "原件下载", "done_download", !state?.inventory_complete)}
    <div className="collection-options">
      <div className="collection-browser-login">
        <button type="button" disabled={busy || running || !state?.connection_configured} onClick={() => void runJob(state?.login || null, "done_login")}>打开浏览器登录</button>
        <span role="status">{state?.login ? state.login.status === "completed" ? "上次登录成功" : LABELS[state.login.status] : "尚未检查登录"}</span>
        <p className="collection-note">在打开的本机 Chrome 中输入账号密码，由浏览器保存。OARadar 复用此浏览器的登录会话。</p>
        {state?.login?.last_error && <p role="alert">{state.login.last_error}</p>}
      </div>
    </div>
  </section>
}
