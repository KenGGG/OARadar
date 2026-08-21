export type ClassificationGroup = "internal" | "external" | "needs_review"

export type RebuildClassificationItem = {
  id: number
  title: string | null
  document_number: string | null
  sender: string | null
  item_date: string | null
  source_type: "internal" | "external" | "unknown" | null
  internal_category: string | null
  external_issuer: string | null
  classification_state: "suggested" | "confirmed" | "needs_review"
  has_document_number: boolean
  attachment_count: number
}

export type RebuildClassificationPage = {
  items: RebuildClassificationItem[]
  total: number
  page: number
  page_size: number
}

export type RebuildClassificationSummary = {
  internal: { suggested: number; confirmed: number; total: number }
  external: { suggested: number; confirmed: number; total: number }
  needs_review: { total: number }
}
