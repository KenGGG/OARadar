"""Local document-type rules, independent of OA source-organization rules."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

VERSION = "markdown-document-tags-v3"


@dataclass(frozen=True)
class DocumentClassification:
    category: str
    document_type: str
    rule_id: str
    needs_review: bool = False

RULES = (
    ("personnel_appointment", "01_人事组织", "任免通知", r"(?:任免|任职|免职|任命|聘任|解聘).{0,18}(?:通知|决定|批复)|(?:通知|决定).{0,12}(?:任免|任职|免职)"),
    ("policy", "02_制度政策", "政策制度", r"(?:印发|发布|修订|实施).{0,60}(?:办法|规定|条例|制度|细则|方案)|(?:管理办法|实施细则|管理规定|指导意见|行动方案)"),
    ("meeting", "03_会议决策", "会议材料", r"(?:会议纪要|会议通知|会议议程|议案|董事会决议|股东(?:会)?决议|党委会决议|评审会决议)"),
    ("finance", "04_财务资金", "财务资金材料", r"(?:财务报告|审计报告|预算报告|决算报告|资金计划|融资方案|财务报表|资产负债表|利润表|现金流量表|付款通知书|放款通知|回款报告)"),
    ("risk", "05_风险合规", "风险合规材料", r"(?:风险报告|风险评估|合规报告|内控报告|整改报告|法律意见书|法审意见|监管反馈)"),
    ("personnel_other", "01_人事组织", "其他人事材料", r"(?:招聘公告|招聘方案|考核结果|绩效考核|培训通知|培训方案|干部考察|选拔任用)"),
    ("publication", "06_刊物学习", "刊物学习资料", r"(?:简报|刊物|专刊|周刊|月刊|学习资料|学习参考|工作动态|信息专报|信息.{0,20}第\d+期)"),
    ("business", "07_业务经营", "业务经营材料", r"(?:项目报告|项目方案|尽职调查|投放方案|租后报告|经营分析|经营报告|工作总结|工作计划)"),
    ("contract", "08_合同协议", "合同协议", r"(?:保证合同|抵押合同|租赁合同|买卖合同|服务合同|服务协议|转让协议|合作协议|借款合同)"),
)

GENERAL_RULES = (
    ("speech", "06_刊物学习", "讲话学习材料", r"讲话|会议精神|学习贯彻"),
    ("notice", "09_行政公文", "一般通知", r"通知"),
    ("announcement", "09_行政公文", "公告通告", r"公告|通告|公示"),
    ("bulletin", "09_行政公文", "工作通报", r"通报"),
    ("letter", "09_行政公文", "函件", r"函(?:\.|$)|确认函|征询函|的函"),
    ("statement", "09_行政公文", "确认书", r"确认书"),
    ("table", "10_数据统计", "统计表格", r"汇总表|填报表|统计表|情况表"),
)


def classify_document(title: str, *, filename: str = "", body: str = "") -> DocumentClassification:
    """Use document headings, never arbitrary later citations in the body."""
    def normalize(value: str) -> str:
        return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))

    # Filename is the attachment's own heading; an aggregate OA title may
    # contain unrelated documents. Prefer that heading when it is conclusive.
    headings = [filename, title]
    if body:
        headings.append("".join(body.splitlines()[:12])[:500])
    for heading in headings:
        text = normalize(heading)
        if re.search(r"(?:业务[一二三四五六七八九十\d]+部|内部|经营).{0,12}(?:工作简报|经营简报|周报)",text):
            return DocumentClassification("业务经营", "工作简报", "internal_work_bulletin")
        found = []
        for identifier, category, kind, pattern in RULES:
            if not re.search(pattern, text):
                continue
            if identifier == "personnel_appointment" and re.search(r"(?:任免|任职|任命).{0,16}(?:办法|规定|制度|细则|条例)", text):
                continue
            found.append(DocumentClassification(category, kind, identifier))
        # A policy about appointments remains a policy, rather than an
        # appointment notification or an incidental personnel keyword hit.
        if any(row.rule_id == "policy" for row in found):
            found = [row for row in found if row.rule_id != "personnel_other"]
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            return DocumentClassification("99_待复核", "混合主题", "conflicting_headings", True)
    for heading in headings:
        text = normalize(heading)
        found = [DocumentClassification(category, kind, identifier)
                 for identifier, category, kind, pattern in GENERAL_RULES if re.search(pattern, text)]
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            return DocumentClassification("99_待复核", "混合主题", "conflicting_headings", True)
    return DocumentClassification("99_待复核", "未确定类型", "insufficient_heading_evidence", True)
