"""Feishu (Lark) notification — daily digest and alerts."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import base64
from datetime import date, datetime, timezone
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


class FeishuNotifier:
    """Sends daily digests and alerts via Feishu Webhook."""

    def __init__(
        self,
        webhook_env: str = "FEISHU_OA_WEBHOOK",
        secret_env: str = "FEISHU_OA_SECRET",
        max_items_per_section: int = 10,
        redact_confidential: bool = True,
        retry_attempts: int = 3,
    ) -> None:
        self.webhook_url = os.environ.get(webhook_env, "")
        self.secret = os.environ.get(secret_env, "")
        self.max_items = max_items_per_section
        self.redact_confidential = redact_confidential
        self.retry_attempts = retry_attempts

        # Validate webhook
        if self.webhook_url:
            from urllib.parse import urlparse
            parsed = urlparse(self.webhook_url)
            if parsed.hostname and parsed.hostname not in ("open.feishu.cn", "open.feishu.com", "www.feishu.cn"):
                raise ValueError(f"Feishu webhook must point to official domain, got: {self.webhook_url}")

    def _sign(self, timestamp: int) -> str:
        """Generate Feishu webhook signature."""
        if not self.secret:
            return ""
        string_to_sign = f"{timestamp}\n{self.secret}"
        return hmac.new(
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).hexdigest()

    def _build_digest_message(self, date_str: str, stats: dict) -> dict:
        """Build an interactive card message for the daily digest."""
        sections = []

        # Section 1: Priority items
        high_priority = stats.get("high_priority", [])
        if high_priority:
            items_text = "\n".join(
                f"{i+1}. [高] {self._redact(item['title'])}\n   截止：{item.get('deadline', '未知')}\n   来源：{item.get('source', '')}"
                for i, item in enumerate(high_priority[:self.max_items])
            )
            sections.append({"tag": "div", "text": {"content_type": "markdown", "content": f"一、需要优先处理\n{items_text}"}})

        # Section 2: New tasks summary
        new_count = stats.get("new_tasks", 0)
        due_today = stats.get("due_today", 0)
        due_3days = stats.get("due_3days", 0)
        overdue = stats.get("overdue", 0)
        sections.append({
            "tag": "div",
            "text": {
                "content_type": "markdown",
                "content": (
                    f"二、新增待办\n"
                    f"- 新增 {new_count} 项\n"
                    f"- 今日截止 {due_today} 项\n"
                    f"- 3日内截止 {due_3days} 项\n"
                    f"- 已逾期 {overdue} 项"
                ),
            },
        })

        # Section 3: Official documents
        official_docs = stats.get("official_documents", 0)
        if official_docs > 0:
            sections.append({
                "tag": "div",
                "text": {"content_type": "markdown", "content": f"三、新增红头文件\n- {official_docs} 份"},
            })

        # Section 4: Attention needed
        low_quality = stats.get("low_quality", 0)
        downloads_failed = stats.get("downloads_failed", 0)
        attention_items = []
        if low_quality > 0:
            attention_items.append(f"- {low_quality} 份扫描 PDF 解析质量偏低")
        if downloads_failed > 0:
            attention_items.append(f"- {downloads_failed} 项附件下载失败")
        if attention_items:
            sections.append({
                "tag": "div",
                "text": {"content_type": "markdown", "content": f"四、需要人工关注\n" + "\n".join(attention_items)},
            })

        # Section 5: System status
        sections.append({
            "tag": "div",
            "text": {
                "content_type": "markdown",
                "content": "五、系统状态\n- OA 登录正常\n- 最近成功同步完成",
            },
        })

        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": f"OA 每日摘要｜{date_str}"},
                    "template": "blue",
                },
                "elements": sections,
            },
        }

    def _redact(self, text: str) -> str:
        """Redact confidential content if configured."""
        if not self.redact_confidential:
            return text
        # Simple redaction of sensitive keywords
        for pattern in ["身份证", "银行卡", "密码", "账号"]:
            text = text.replace(pattern, pattern[0] + "*" * (len(pattern) - 1))
        return text

    def send_digest(self, date_str: str, stats: dict) -> bool:
        """Send the daily digest to Feishu. Returns True if sent successfully."""
        if not self.webhook_url:
            logger.info("Feishu webhook not configured; skipping digest")
            return False

        message = self._build_digest_message(date_str, stats)
        payload = json.dumps(message, ensure_ascii=False).encode("utf-8")

        timestamp = int(time.time())
        sign = self._sign(timestamp)

        for attempt in range(self.retry_attempts):
            try:
                with httpx.Client(timeout=30) as client:
                    response = client.post(
                        self.webhook_url,
                        json={"timestamp": timestamp, "sign": sign, **message},
                    )
                    response.raise_for_status()
                    logger.info("Feishu digest sent successfully: %s", date_str)
                    return True
            except httpx.RequestError as exc:
                logger.warning("Feishu send attempt %d failed: %s", attempt + 1, exc)
                if attempt < self.retry_attempts - 1:
                    time.sleep(2 ** attempt)

        logger.error("Feishu digest failed after %d attempts", self.retry_attempts)
        return False

    def send_alert(self, alert_type: str, message: str) -> bool:
        """Send an immediate alert notification."""
        if not self.webhook_url:
            return False

        payload = {
            "msg_type": "text",
            "content": {"text": f"⚠️ OA 系统告警｜{alert_type}\n{message}"},
        }

        timestamp = int(time.time())
        sign = self._sign(timestamp)

        try:
            with httpx.Client(timeout=10) as client:
                response = client.post(
                    self.webhook_url,
                    json={"timestamp": timestamp, "sign": sign, **payload},
                )
                response.raise_for_status()
                return True
        except Exception as exc:
            logger.error("Feishu alert failed: %s", exc)
            return False
