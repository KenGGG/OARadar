"""Declarative, local-only second-pass title rules. No downloaded code executes."""
from __future__ import annotations

import re
import unicodedata

FIELDS = {"clean", "raw", "head"}
KINDS = {"workflow", "series", "issuer", "joint", "theme", "review"}


def validate_condition(node: dict, depth: int = 0) -> None:
    if depth > 12 or not isinstance(node, dict):
        raise ValueError("补充规则条件过深或格式错误")
    operation = node.get("op")
    if operation in {"all", "any"}:
        if not isinstance(node.get("conditions"), list) or not node["conditions"]:
            raise ValueError("组合条件不可为空")
        for child in node["conditions"]:
            validate_condition(child, depth + 1)
    elif operation == "not":
        validate_condition(node["condition"], depth + 1)
    elif operation == "transmission":
        pass
    elif operation in {"regex", "prefix", "contains", "count_gte"}:
        if node.get("field") not in FIELDS or not isinstance(node.get("value"), str) or not node["value"]:
            raise ValueError("匹配字段和表达式不可为空")
        if operation == "regex":
            re.compile(node["value"])
        if operation == "count_gte" and not isinstance(node.get("count"), int):
            raise ValueError("次数条件须为整数")
    else:
        raise ValueError("不支持的补充规则条件")


def validate_second_pass(spec: dict) -> None:
    if not isinstance(spec.get("enabled", False), bool):
        raise ValueError("补充规则启用状态须为布尔值")
    identifiers = set()
    for rule in spec.get("rules", []):
        if rule["id"] in identifiers:
            raise ValueError("补充规则编号重复")
        identifiers.add(rule["id"])
        if rule.get("kind") not in KINDS or rule.get("category") not in {"SELF", "EXTERNAL", "PUBLICATION", "UNKNOWN"}:
            raise ValueError("不支持的补充规则类别")
        if not isinstance(rule.get("enabled", True), bool):
            raise ValueError("规则启用状态须为布尔值")
        if rule["kind"] == "workflow" and rule["category"] != "SELF":
            raise ValueError("业务流程只能归本公司")
        if rule["kind"] == "series" and (rule["category"] != "PUBLICATION" or not rule.get("label")):
            raise ValueError("刊物须指定系列")
        if rule["kind"] in {"issuer", "joint", "theme"} and (rule["category"] != "EXTERNAL" or not rule.get("label")):
            raise ValueError("外部归集须指定目录名称")
        if rule["kind"] == "review" and rule["category"] != "UNKNOWN":
            raise ValueError("人工审核建议必须保持待确认")
        validate_condition(rule["condition"])


def normalized_fields(title: str) -> dict:
    raw = re.sub(r"\s+", "", unicodedata.normalize("NFKC", title))
    clean = re.sub(r"\(由[^()]{1,30}原发\)$", "", raw)
    for _ in range(10):
        value = re.sub(r"^(?:【(?:文件传阅|公告|工作通报|阅件|以此为准|工作资料,请勿外传|多文合办)】|\((?:以此为准(?:,补充正文)?|盖章版|转发|两文合办|三文合办|[一二三四五六七八九十\d]+文合办)\))", "", clean)
        if value == clean:
            break
        clean = value
    return {"raw": raw, "clean": clean, "head": clean.split("关于", 1)[0][:150],
            "transmission": raw.startswith(("【文件传阅】", "【传阅", "【阅件"))}


def matches(node: dict, fields: dict) -> bool:
    operation = node["op"]
    if operation == "all":
        return all(matches(child, fields) for child in node["conditions"])
    if operation == "any":
        return any(matches(child, fields) for child in node["conditions"])
    if operation == "not":
        return not matches(node["condition"], fields)
    if operation == "transmission":
        return fields["transmission"]
    value, pattern = fields[node["field"]], node["value"]
    if operation == "regex":
        return bool(re.search(pattern, value))
    if operation == "prefix":
        return value.startswith(pattern)
    if operation == "contains":
        return pattern in value
    return value.count(pattern) >= node["count"]


def suggest(spec: dict, title: str, *, explicit_body: bool = False) -> dict | None:
    if not spec.get("enabled") or explicit_body:
        return None
    fields = normalized_fields(title)
    for rule in spec.get("rules", []):
        if not rule.get("enabled", True) or not matches(rule["condition"], fields):
            continue
        # A forwarded regulation mentioning an approval flow is not our workflow.
        if rule["kind"] == "workflow" and (fields["transmission"] or (
            rule.get("source_rule_id") == "N08" and re.match(r"关于(?:印发|转发|发布|学习|贯彻)", fields["clean"]))):
            continue
        return rule
    return None
