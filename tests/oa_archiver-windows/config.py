from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG: dict[str, Any] = {
    "oa_url": "https://oa.example.invalid/",
    "output_dir": "OA归档",
    "browser": {
        "channel": "msedge",
        "headless": False,
        "executable_path": "",
        "user_data_dir": ".browser-profile",
        "downloads_dir": "OA归档/_staging",
        "slow_mo_ms": 0,
        "ignore_https_errors": True,
    },
    "download": {
        "per_file_timeout_seconds": 600,
        "attachment_total_timeout_seconds": 900,
        "direct_download_chunk_size": 1048576,
        "stall_timeout_seconds": 30,
        "progress_log_interval_seconds": 10,
        "related_event_timeout_ms": 1000,
        "related_max_links": 2,
        "related_scan_max": 30,
        "direct_link_scan_max": 120,
    },
    "navigation": {
        "done_url": "https://oa.example.invalid/oa/done",
        "menu_texts": ["协同工作", "已办事项"],
    },
    "selectors": {
        "done_list_rows": ["table tr:has(.titleText)"],
        "title_links": ["a:visible"],
        "attachments": [
            "div.attachment_block",
        ],
        "detail_text_container": ["body"],
    },
    "list_fields": {
        "title": {"headers": ["标题", "事项", "名称"], "fallback_index": 1},
        "sender": {"headers": ["发文单位", "来文单位", "发送人", "发起人"], "fallback_index": 2},
        "doc_no": {"headers": ["文号", "编号"], "fallback_index": -1},
        "handled_at": {"headers": ["办理时间", "完成时间", "时间"], "fallback_index": 5},
        "draft_department": {"headers": ["拟稿部门", "拟稿单位", "部门"], "fallback_index": -1},
    },
}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return DEFAULT_CONFIG
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return deep_merge(DEFAULT_CONFIG, data)
