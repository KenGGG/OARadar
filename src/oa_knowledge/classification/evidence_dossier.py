"""Per-OA evidence assembly and primary-document selection.

Selection is deliberately scoped to attachments belonging to one OA item.  A
content hash may identify a cached parse product, but never carries a
classification label between OA items.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from oa_knowledge.config import Settings
from oa_knowledge.db.models import (
    ArchivedFile,
    ClassificationDecision,
    OAItem,
    OAManifestItem,
)

from .metadata_rules import normalize_person
from .schemas import PrivateClassificationConfig
from .semantic_package_loader import DatabaseSemanticPackageLoader


@dataclass(frozen=True, slots=True)
class AttachmentEvidence:
    file_id: int
    name: str
    role: str
    content_sha256: str | None
    text: str | None
    header_excerpt: str | None
    body_excerpt: str | None
    signature_excerpt: str | None


@dataclass(frozen=True, slots=True)
class OAEvidenceDossier:
    oa_item_key: str
    current_decision_id: int
    title: str
    normalized_title: str
    initiator: str | None
    initiator_profile: str
    relay_from: str | None
    workflow_name: str | None
    form_name: str | None
    document_number: str | None
    no_attachment_confirmed: bool
    attachments: tuple[AttachmentEvidence, ...]
    primary_attachment: AttachmentEvidence | None
    primary_attachment_reason: str
    primary_attachment_conflict: bool


@dataclass(frozen=True, slots=True)
class PrimaryAttachmentSelection:
    attachment: AttachmentEvidence | None
    reason: str
    conflict: bool


_INTERNAL_FORM = re.compile(
    r"内部事项呈批|印鉴(?:使用)?申请|印章使用申请|用印申请|部门章使用申请|"
    r"(?:业务|采购)?合同审批|(?:办公会|董事会)议题申请|业务出账申请|"
    r"资金划转审批|项目(?:立项|预审|投放).*申请|预审会决议|租后检查|"
    r"资产分类|采购过程文件审核"
)
_FORM_ROLES = {"body_snapshot", "workflow_snapshot", "metadata_snapshot"}
_FORMAL_DOCUMENT = re.compile(r"通知|函|决定|通报|批复|意见|办法|公告")
_VERSION_WORDS = re.compile(
    r"(?:附件\s*[一二三四五六七八九十\d]+|盖章版|最终版|定稿|扫描件|正文)"
)


def select_primary_attachment(
    title: str,
    content_origin_hint: Literal["internal", "external"] | None,
    attachments: tuple[AttachmentEvidence, ...],
) -> PrimaryAttachmentSelection:
    """Select one primary file from this OA only, or return an explicit conflict."""
    if not attachments:
        return PrimaryAttachmentSelection(None, "no_attachment", False)

    scored = [
        (_attachment_score(title, content_origin_hint, attachment), attachment)
        for attachment in attachments
    ]
    best_score = max(score for score, _ in scored)
    winners = [attachment for score, attachment in scored if score == best_score]
    if len(winners) != 1:
        return PrimaryAttachmentSelection(None, "multiple_primary_candidates", True)

    winner = winners[0]
    if (
        content_origin_hint == "internal"
        and winner.role not in _FORM_ROLES
        and not _INTERNAL_FORM.search(winner.name)
        and not _title_match(title, winner.name)
    ):
        return PrimaryAttachmentSelection(None, "no_reliable_primary_attachment", True)
    if content_origin_hint == "internal" and winner.role in _FORM_ROLES:
        reason = "internal_form_or_body"
    elif content_origin_hint is None and winner.role in _FORM_ROLES:
        reason = "oa_detail_or_form"
    elif content_origin_hint == "external" and winner.role == "official_body":
        reason = "external_official_body"
    elif content_origin_hint == "external" and _title_match(title, winner.name):
        reason = "external_title_match"
    else:
        reason = "best_same_oa_attachment"
    return PrimaryAttachmentSelection(winner, reason, False)


def _attachment_score(
    title: str,
    content_origin_hint: str | None,
    attachment: AttachmentEvidence,
) -> tuple[int, int, int]:
    role_score = 0
    if content_origin_hint == "internal":
        if attachment.role in _FORM_ROLES:
            role_score = 120
        elif _INTERNAL_FORM.search(attachment.name):
            role_score = 110
    elif content_origin_hint == "external":
        if attachment.role == "official_body":
            role_score = 140
        elif attachment.role == "direct_attachment":
            role_score = 40
        elif attachment.role == "official_attachment":
            role_score = 20
    elif attachment.role in _FORM_ROLES:
        role_score = 80

    title_score = 100 if _title_match(title, attachment.name) else 0
    formal_score = 10 if _FORMAL_DOCUMENT.search(attachment.name) else 0
    return role_score, title_score, formal_score


def _title_match(title: str, filename: str) -> bool:
    return _normalise_title(title) == _normalise_title(filename)


def _normalise_title(value: str) -> str:
    stem = Path(value).stem
    stem = re.sub(r"【[^】]+】", "", stem)
    stem = re.sub(r"[（(]由[^）)]+原发[）)]", "", stem)
    stem = _VERSION_WORDS.sub("", stem)
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", stem).lower()


class DatabaseEvidenceDossierLoader:
    """Build one dossier after selecting a primary file from that OA's names."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        settings: Settings,
        config: PrivateClassificationConfig,
    ) -> None:
        self._sessions = session_factory
        self._config = config
        self._text_loader = DatabaseSemanticPackageLoader(session_factory, settings)

    def __call__(self, oa_item_key: str, *, load_text: bool = True) -> OAEvidenceDossier:
        with self._sessions() as session:
            item = session.scalar(select(OAItem).where(OAItem.oa_item_key == oa_item_key))
            manifest = session.scalar(
                select(OAManifestItem).where(OAManifestItem.oa_item_key == oa_item_key)
            )
            decision = session.scalar(
                select(ClassificationDecision).where(
                    ClassificationDecision.oa_item_key == oa_item_key,
                    ClassificationDecision.is_current.is_(True),
                )
            )
            if item is None or manifest is None or decision is None:
                raise ValueError("OA dossier requires item, manifest, and current decision")
            files = list(
                session.scalars(
                    select(ArchivedFile)
                    .where(
                        ArchivedFile.oa_item_id == item.id,
                        ArchivedFile.download_status == "verified",
                    )
                    .order_by(ArchivedFile.id)
                )
            )
            attachments = tuple(
                AttachmentEvidence(
                    file_id=file.id,
                    name=file.original_name,
                    role=file.file_role,
                    content_sha256=file.sha256,
                    text=None,
                    header_excerpt=None,
                    body_excerpt=None,
                    signature_excerpt=None,
                )
                for file in _deduplicate_files(files)
            )
            initiator = decision.initiator or item.sender
            profile = _initiator_profile(initiator, self._config)
            document_number = decision.document_number or item.document_number
            hint = _preliminary_origin_hint(
                item.title, document_number, self._config
            )
            selection = select_primary_attachment(item.title, hint, attachments)

            if not attachments:
                selection = PrimaryAttachmentSelection(
                    None,
                    "no_attachment_confirmed"
                    if manifest.no_attachment_confirmed
                    else "attachment_unavailable",
                    not manifest.no_attachment_confirmed,
                )

        selected = selection.attachment
        if selected is not None and load_text:
            loaded = self._text_loader.load_file_text(selected.file_id)
            if loaded is not None:
                text, content_sha = loaded
                selected = AttachmentEvidence(
                    file_id=selected.file_id,
                    name=selected.name,
                    role=selected.role,
                    content_sha256=content_sha,
                    text=_bounded_text(text, 16_000),
                    header_excerpt=text[:4_000].strip() or None,
                    body_excerpt=_bounded_text(text, 8_000),
                    signature_excerpt=text[-4_000:].strip() or None,
                )
                attachments = tuple(
                    selected if entry.file_id == selected.file_id else entry
                    for entry in attachments
                )
            else:
                selection = PrimaryAttachmentSelection(selected, "parse_failed", True)

        return OAEvidenceDossier(
            oa_item_key=oa_item_key,
            current_decision_id=decision.id,
            title=item.title,
            normalized_title=_normalise_title(item.title),
            initiator=initiator,
            initiator_profile=profile,
            relay_from=decision.relay_from,
            workflow_name=None,
            form_name=None,
            document_number=document_number,
            no_attachment_confirmed=manifest.no_attachment_confirmed,
            attachments=attachments,
            primary_attachment=selected,
            primary_attachment_reason=selection.reason,
            primary_attachment_conflict=selection.conflict,
        )


def _deduplicate_files(files: list[ArchivedFile]) -> tuple[ArchivedFile, ...]:
    selected: dict[str, ArchivedFile] = {}
    for file in files:
        key = file.sha256 or f"file:{file.id}"
        prior = selected.get(key)
        if prior is None or len(file.original_name) > len(prior.original_name):
            selected[key] = file
    return tuple(sorted(selected.values(), key=lambda file: file.id))


def _initiator_profile(
    initiator: str | None, config: PrivateClassificationConfig
) -> str:
    if not initiator:
        return "unknown"
    person, _organization = normalize_person(initiator)
    key = person.casefold()
    for identifier, profile in config.initiators.items():
        aliases = (identifier, *profile.aliases)
        if any(normalize_person(alias)[0].casefold() == key for alias in aliases):
            return profile.role
    return "unknown"


def _preliminary_origin_hint(
    title: str,
    document_number: str | None,
    config: PrivateClassificationConfig,
) -> Literal["internal", "external"] | None:
    evidence = "\n".join((title, document_number or ""))
    for rule in config.document_number_issuers:
        if re.search(rule.pattern, evidence):
            return (
                "internal"
                if rule.canonical_issuer == "广州凯得融资租赁有限公司"
                else "external"
            )
    if _INTERNAL_FORM.search(title):
        return "internal"
    if re.search(r"文件传阅|传阅件|【传阅】", title):
        return "external"
    return None


def _bounded_text(value: str, maximum: int) -> str:
    text = value.strip()
    if len(text) <= maximum:
        return text
    half = maximum // 2
    return f"{text[:half].rstrip()}\n\n[中间内容已截断]\n\n{text[-half:].lstrip()}"
