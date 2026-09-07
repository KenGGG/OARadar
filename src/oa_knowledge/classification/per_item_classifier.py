"""Deterministic classification of one OA from that OA's own evidence only."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from .evidence_dossier import OAEvidenceDossier
from .internal_classification import BusinessCategory, classify_by_content
from .metadata_rules import resolve_configured_document_issuer, resolve_issuer_from_text
from .schemas import PrivateClassificationConfig

_SELF_ISSUER = "广州凯得融资租赁有限公司"
CLASSIFIER_VERSION = "per-item-v2"
_FILE_TRANSFER = re.compile(r"文件传阅|传阅件|【传阅】")
_STRONG_INTERNAL = re.compile(
    r"内部事项呈批|印鉴(?:使用)?申请|印章使用申请|用印申请|部门章使用申请|"
    r"(?:业务|采购)?合同审批|(?:办公会|董事会)议题申请|业务出账申请|"
    r"项目(?:立项|预审|评审|投放).*?(?:申请|决议)?|出账申请|租后检查|"
    r"资产分类|采购(?:过程)?文件审核|项目[^。\n]{0,40}尽职调查的?内部请示"
)
_SELF_INTERNAL_DOCUMENT = re.compile(
    r"^(?:广州凯得融资租赁有限公司|凯得融资租赁|凯得租赁).{0,40}(?:制度|办法|通知|决议|通讯录|董事会会议材料|发展定位|发展目标|经营规划)"
)
_OUTBOUND = re.compile(r"(?:报送|回复|反馈|填报).*(?:集团|监管|政府|主管部门)")
_BUSINESS_DISBURSEMENT = re.compile(r"业务出账|项目出账|租金支付")
_DOCUMENT_NUMBER = re.compile(
    r"[A-Za-z\u4e00-\u9fff]{1,24}\s*[〔［【\[]\s*\d{4}\s*[〕］】\]]\s*\d{1,6}\s*号"
)
_ISSUER_END = r"(?:国务院|委员会|委员会办公室|人民政府(?:办公室)?|人民法院|人民检察院|工作领导小组办公室|联席会议办公室|管理委员会(?:办公室)?|(?:[\u4e00-\u9fff]{2,12})(?:局|厅|部|办|中心|集团(?:有限公司)?|公司|纪委|党委|工会))"
_FORMAL_ISSUER_TITLE = re.compile(
    r"(?:^|\n|【[^】]+】)\s*(?P<issuer>[^\n]{2,100}?)\s*(?:关于|(?:纪检监察|工作)?(?:简报|专刊|信息|动态))"
)
_HEADER_ISSUER = re.compile(
    r"(?:^|\n)\s*(?P<issuer>[^\n]{2,100})\s*\n[^\n]{0,100}(?:通知|函|决定|通报|批复|意见|办法|公告|简报|专刊|信息|动态)"
)
_EXPLICIT_ISSUER = re.compile(
    rf"(?:发文机关|发文单位|落款)\s*[:：]\s*(?P<issuer>[\u4e00-\u9fff、，,（）()\s]{{2,80}}?{_ISSUER_END})(?:\s|$)"
)
_SIGNATURE_ISSUER = re.compile(
    rf"(?:^|\n)\s*(?P<issuer>[\u4e00-\u9fff、，,（）()\s]{{2,80}}?{_ISSUER_END})\s*(?:\n|$)"
)
_INTERNAL_DEPARTMENT = re.compile(
    r"(?:业务|综合(?:管理)?|财务(?:资金)?|人力资源|风险合规|行政|编研)[一二三四五六七八九十\d]*(?:部|中心)$"
)


@dataclass(frozen=True, slots=True)
class ProposedClassification:
    classification_status: Literal["classified", "needs_review"]
    content_origin: Literal["internal", "external"] | None
    business_category: BusinessCategory | None
    raw_issuer: str | None
    canonical_issuer: str | None
    document_number: str | None
    document_type: str | None
    decision_source: str
    confidence: float
    origin_evidence_source: str
    origin_evidence_quote: str
    classification_evidence_source: str
    classification_evidence_quote: str
    review_reason: str | None
    origin_conflict: str | None = None


def classify_dossier(
    dossier: OAEvidenceDossier,
    config: PrivateClassificationConfig,
) -> ProposedClassification:
    """Classify one dossier without consulting another OA decision."""
    texts = _dossier_texts(dossier)
    document_match = _direct_document_match(dossier, config)
    issuer_evidence = _strong_external_issuer(dossier, config)
    issuer = issuer_evidence[0] if issuer_evidence is not None else None
    internal_lock = _strong_internal_lock(dossier, document_match, issuer_evidence)
    conflict = None

    if internal_lock is not None:
        origin, origin_source, origin_quote = "internal", internal_lock[0], internal_lock[1]
        if dossier.primary_attachment_conflict:
            conflict = "internal_process_vs_supporting_attachment_conflict"
        elif (
            (document_match is not None and document_match[1] != _SELF_ISSUER)
            or (issuer_evidence is not None and issuer_evidence[0] != _SELF_ISSUER)
        ):
            conflict = "internal_process_vs_external_supporting_document"
    elif document_match is not None:
        origin, origin_source, origin_quote = "external", document_match[3], document_match[0]
        issuer = document_match[1]
    elif _FILE_TRANSFER.search(dossier.title):
        match = _FILE_TRANSFER.search(dossier.title)
        origin, origin_source, origin_quote = "external", "title_rule", match.group(0)  # type: ignore[union-attr]
    elif issuer_evidence is not None:
        if issuer_evidence[0] == _SELF_ISSUER:
            origin, origin_source, origin_quote = "internal", issuer_evidence[1], issuer_evidence[2]
        else:
            origin, origin_source, origin_quote = "external", issuer_evidence[1], issuer_evidence[2]
    else:
        return _review(None, "origin_unresolved", dossier)

    if origin == "external":
        if issuer is None:
            return _review("external", "issuer_missing", dossier, origin_source, origin_quote)
        document_type = document_match[2] if document_match else None
        return ProposedClassification(
            "classified", "external", None, issuer, issuer,
            document_match[0] if document_match else dossier.document_number,
            document_type, origin_source, 0.99, origin_source, origin_quote,
            origin_source, issuer, None, conflict,
        )

    internal = classify_by_content(dossier.title, texts)
    category = internal.business_category if internal is not None else _special_internal_category(dossier.title)
    if category is None:
        return _review("internal", "business_category_missing", dossier, origin_source, origin_quote)
    evidence = internal.evidence if internal is not None else _special_internal_evidence(dossier.title)
    return ProposedClassification(
        "classified", "internal", category, None, None, dossier.document_number,
        internal.document_type if internal else None,
        "content_rule", 0.96, origin_source, origin_quote,
        "title_rule" if evidence and evidence in dossier.title else "content_rule",
        evidence, None, conflict,
    )


def _strong_internal_lock(
    dossier: OAEvidenceDossier,
    document_match: tuple[str, str, str | None, str] | None,
    issuer_evidence: tuple[str, str, str] | None,
) -> tuple[str, str] | None:
    match = _STRONG_INTERNAL.search(dossier.title)
    if match is not None:
        return "title_rule", match.group(0)
    own_title = _SELF_INTERNAL_DOCUMENT.search(dossier.title)
    if own_title is not None:
        return "title_rule", own_title.group(0)[:80]
    if document_match is not None and document_match[1] == _SELF_ISSUER:
        return document_match[3], document_match[0]
    if issuer_evidence is not None and issuer_evidence[0] == _SELF_ISSUER:
        return issuer_evidence[1], issuer_evidence[2]
    # An initiator is only weak context; it cannot reverse the direct issuer
    # shown by this OA's own title/header/signature.
    if issuer_evidence is not None:
        return None
    if dossier.initiator_profile == "internal" and not _FILE_TRANSFER.search(dossier.title):
        subject = classify_by_content(dossier.title, ())
        if subject is not None:
            return "title_and_initiator", subject.evidence
    return None


def _strong_external_issuer(
    dossier: OAEvidenceDossier,
    config: PrivateClassificationConfig,
) -> tuple[str, str, str] | None:
    """Require issuer context, not merely an organization name anywhere."""
    attachment = dossier.primary_attachment
    sources = (
        ("title_rule", dossier.title),
        ("attachment_filename", attachment.name if attachment else None),
        ("document_header", attachment.header_excerpt if attachment else None),
        ("signature", attachment.signature_excerpt if attachment else None),
    )
    for source, value in sources:
        if not value:
            continue
        candidate = _issuer_from_source(source, value)
        if candidate is None:
            continue
        canonical = _canonical_issuer(candidate, config)
        if canonical is not None:
            return canonical, source, candidate
    return None


def _issuer_from_source(source: str, value: str) -> str | None:
    """Extract only a direct issuer position, never an organization merely cited."""
    patterns = (_EXPLICIT_ISSUER, _FORMAL_ISSUER_TITLE)
    if source == "document_header":
        patterns += (_HEADER_ISSUER,)
    if source == "signature":
        patterns += (_SIGNATURE_ISSUER,)
    for pattern in patterns:
        if match := pattern.search(value):
            candidate = _outer_issuer(_clean_issuer(match.group("issuer")))
            if _is_formal_issuer(candidate):
                return candidate
    return None


def _clean_issuer(value: str) -> str:
    value = value.strip()
    while True:
        cleaned = re.sub(r"^(?:【[^】]+】|[（(](?:新增|[一二三四五\d]+次办理)[^）)]*[）)]|[（(](?:盖章版|扫描件|原件|复印件|定稿|正式稿|最终版)[）)])\s*", "", value)
        if cleaned == value:
            break
        value = cleaned
    value = re.sub(r"^(?:新增[^：:。\n]{1,20}批示|[一二三四五\d]+次办理)\s*[:：、，,\-—]*\s*", "", value)
    return re.sub(r"[\s，,、]+", "、", value).strip("、")


def _outer_issuer(value: str) -> str:
    """Keep the current forwarding issuer; upstream issuers remain evidence only."""
    return value.split("转发", 1)[0].strip("、，,")


def _is_formal_issuer(value: str) -> bool:
    if "、" in value:
        return all(_is_formal_issuer(part) for part in value.split("、"))
    if re.search(r"批示|办理|收件|路演厅|会议室", value):
        return False
    if value.startswith(("根据", "贯彻", "转发", "落实", "参照", "按照")):
        return False
    if _INTERNAL_DEPARTMENT.fullmatch(value):
        return False
    return re.fullmatch(
        rf"(?:{_ISSUER_END}|[\u4e00-\u9fff、，,（）()]+{_ISSUER_END})", value
    ) is not None


def _canonical_issuer(
    raw_issuer: str, config: PrivateClassificationConfig
) -> str | None:
    """Aliases normalize spelling; a formal name is self-canonical evidence."""
    if "、" in raw_issuer:
        parts = [_canonical_issuer(part, config) for part in raw_issuer.split("、")]
        return "、".join(parts) if all(parts) else None
    if raw_issuer == _SELF_ISSUER:
        return _SELF_ISSUER
    resolved = resolve_issuer_from_text(raw_issuer, (), config)
    if resolved is not None:
        return resolved
    if _is_formal_issuer(raw_issuer):
        return raw_issuer
    return None


def _direct_document_match(
    dossier: OAEvidenceDossier, config: PrivateClassificationConfig
) -> tuple[str, str, str | None, str] | None:
    attachment = dossier.primary_attachment
    for source, value in (
        ("document_number", dossier.document_number),
        ("document_number", dossier.title),
        ("document_header", attachment.header_excerpt if attachment else None),
    ):
        if value and (match := _configured_document_match(value, config)) is not None:
            return (*match, source)
    return None


def _configured_document_match(
    text: str, config: PrivateClassificationConfig
) -> tuple[str, str, str | None] | None:
    external = resolve_configured_document_issuer(text, config)
    if external is not None:
        return _clean_document_match(external)
    for rule in config.document_number_issuers:
        match = re.search(rule.pattern, text)
        if match is not None:
            return _clean_document_match(
                (match.group(0), rule.canonical_issuer, rule.document_type)
            )
    return None


def _clean_document_match(
    match: tuple[str, str, str | None]
) -> tuple[str, str, str | None]:
    raw, issuer, document_type = match
    token = _DOCUMENT_NUMBER.search(raw)
    quote = token.group(0) if token is not None else " ".join(raw.split())[:80]
    return quote[:80], issuer, document_type


def _dossier_texts(dossier: OAEvidenceDossier) -> tuple[str, ...]:
    attachment = dossier.primary_attachment
    if attachment is None:
        return ()
    ordered = (
        attachment.header_excerpt,
        attachment.body_excerpt,
        attachment.signature_excerpt,
    )
    return tuple(dict.fromkeys(value.strip() for value in ordered if value and value.strip()))


def _special_internal_category(title: str) -> BusinessCategory | None:
    if _BUSINESS_DISBURSEMENT.search(title):
        return "02_业务项目与投放租后"
    if _OUTBOUND.search(title):
        return "09_对外报送与监管反馈"
    return None


def _special_internal_evidence(title: str) -> str:
    match = _BUSINESS_DISBURSEMENT.search(title) or _OUTBOUND.search(title)
    return match.group(0) if match is not None else ""


def _review(
    origin: Literal["internal", "external"] | None,
    reason: str,
    dossier: OAEvidenceDossier,
    source: str = "none",
    quote: str = "",
) -> ProposedClassification:
    return ProposedClassification(
        "needs_review", origin, None, None, None, dossier.document_number, None,
        "deterministic", 0.0, source, quote, "none", "", reason,
    )
