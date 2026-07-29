"""Read-only identity extraction from a Seeyon pending detail page."""

from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import Page


@dataclass(frozen=True)
class PendingDetailIdentifiers:
    affair_id_text: str | None
    summary_id_text: str | None
    process_id_text: str | None
    activity_id_text: str | None
    case_id_text: str | None
    workitem_id_text: str | None
    form_record_id_text: str | None
    object_id_text: str | None
    template_id_text: str | None


def extract_pending_detail_identifiers(page: Page) -> PendingDetailIdentifiers:
    values = page.evaluate(
        """() => {
          const first = (...names) => {
            for (const name of names) {
              const value = window[name];
              if (typeof value === 'string' || typeof value === 'number') {
                const text = String(value).trim();
                if (text) return text;
              }
            }
            return null;
          };
          return {
            affair_id_text: first('affairId', 'contentAffairId'),
            summary_id_text: first('summaryId', '_summaryId'),
            process_id_text: first('_summaryProcessId', '_contextProcessId', 'processId', 'wfProcessId'),
            activity_id_text: first('_summaryActivityId', '_contextActivityId', 'wfActivityId'),
            case_id_text: first('_summaryCaseId', '_contextCaseId', 'wfCaseId'),
            workitem_id_text: first('_summaryItemId', 'workItemId'),
            form_record_id_text: first('formRecordid', 'formRecordId'),
            object_id_text: first('objectId', 'baseObjectId'),
            template_id_text: first('templateId', '_processTemplateId')
          };
        }"""
    )
    return PendingDetailIdentifiers(**values)
