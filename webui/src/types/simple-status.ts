// 极简状态接口（/api/simple-status）与已办资料响应类型（spec §4-§5）。
// 严格按后端聚合口径声明，前端只渲染这些业务字段，不拼接原始技术状态。
// 禁止 any。

export type BusinessTone = "normal" | "working" | "attention" | "fallback_used" | "completed" | "unknown"

export type SimpleDoneState =
  | "waiting_download"
  | "waiting_markdown"
  | "waiting_classification"
  | "completed"
  | "attention"
  | "excluded"

export type SimpleDoneFilter = SimpleDoneState | "no_attachment"

export type AttentionSeverity = "error" | "warning"

export type AttentionJump = "done" | "settings"

export interface SimpleDoneSummary {
  status: BusinessTone
  headline: string
  oa_total: number
  archive_complete: number
  waiting_download_items: number
  download_issue_items: number
  excluded: number
  no_attachment: number
  markdown_ready_items: number
  published_items: number
  queued_items: number
  running_items: number
  failed_items: number
  review_items: number
  last_scan_at: string | null
}

export interface SimplePendingSummary {
  status: BusinessTone
  headline: string
  frequency_text: string
  last_scan_at: string | null
  next_scan_at: string | null
  oa_pending_count: number
  model_name: string
  model_success: number
  model_fallback: number
  model_failed: number
  feishu_sent: number
  feishu_failed: number
  feishu_unknown: number
  last_feishu_success_at: string | null
}

export type OaActivityStatus =
  | "logging_in"
  | "disconnected"
  | "working"
  | "authenticated"
  | "unknown"

export interface SimpleOaActivity {
  status: OaActivityStatus
  label: string
  detail: string
  heartbeat_at: string | null
  progress_current: number | null
  progress_total: number | null
}

export interface SimpleAttentionItem {
  label: string
  severity: AttentionSeverity
  jump: AttentionJump
  filter?: string
}

export interface SimpleStatusResponse {
  generated_at: string
  overall_status: BusinessTone
  done: SimpleDoneSummary
  pending: SimplePendingSummary
  oa_activity: SimpleOaActivity
  attention: SimpleAttentionItem[]
}

export interface SimpleDoneItem {
  id: number
  item_id: string
  title: string
  sender: string | null
  initiated_at: string | null
  completed_at: string | null
  pipeline_status: string
  archive_relpath: string | null
  file_count: number | null
  attachment_names: string[]
  attachment_review_label: string | null
  simple_status: SimpleDoneState
  simple_status_label: string
  attention_reason: string | null
  updated_at: string | null
}

export interface SimpleDonePage {
  items: SimpleDoneItem[]
  total: number
  page: number
  page_size: number
  metrics: { oa_done_total: number; downloaded_items: number; verified_attachments: number }
  lifecycle_pilot_status: string
}
