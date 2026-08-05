"""Single source of truth for OA knowledge-source file roles.

Several modules previously maintained their own role tuples
(``markdown_queue.ATTACHMENT_ROLES``, ad-hoc literals in ``cli.py`` and
``done_knowledge.py``), which drifted: ``done_knowledge`` treated five roles as
knowledge sources while ``markdown_queue`` only enqueued three. Every producer,
auditor and queue now imports from here so a role added in one place is
honored everywhere (plan-0805-02 §1.4).
"""

from __future__ import annotations

# Roles that represent downloadable OA artifacts worth keeping on disk.
RAW_ATTACHMENT_ROLES: tuple[str, ...] = (
    "direct_attachment",
    "official_body",
    "official_attachment",
    "associated_document",
    "opinion_attachment",
)

# Roles that constitute an OA item's knowledge content (fed to the Markdown queue).
KNOWLEDGE_SOURCE_ROLES: tuple[str, ...] = RAW_ATTACHMENT_ROLES

# Roles that must be converted to Markdown. All knowledge sources are converted.
MARKDOWN_SOURCE_ROLES: tuple[str, ...] = RAW_ATTACHMENT_ROLES

# Roles the online audit counts as attachments for completeness checks.
AUDIT_ATTACHMENT_ROLES: tuple[str, ...] = RAW_ATTACHMENT_ROLES
