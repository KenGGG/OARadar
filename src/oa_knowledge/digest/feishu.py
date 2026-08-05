"""Feishu (Lark) notification — daily digest and alerts."""

from __future__ import annotations

import hashlib
import hmac
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
        """Generate Feishu webhook signature.

        Feishu custom bots require HMAC-SHA256 over ``timestamp + "\\n" + secret``
        with an empty message body, then Base64-encoded (not hex). See the Feishu
        open-platform signing spec.
        """
        if not self.secret:
            return ""
        string_to_sign = f"{timestamp}\n{self.secret}"
        digest = hmac.new(
            string_to_sign.encode("utf-8"),
            msg=b"",
            digestmod=hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("utf-8")

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

    def _post(self, message: dict, *, timeout: float = 30) -> None:
        """POST a message to the Feishu webhook, raising on rejection or transport error.

        Raises ``httpx.RequestError`` / ``httpx.HTTPStatusError`` for transport problems
        and ``RuntimeError`` when Feishu returns a non-zero business code.
        """
        timestamp = int(time.time())
        sign = self._sign(timestamp)
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                self.webhook_url,
                json={"timestamp": timestamp, "sign": sign, **message},
            )
            response.raise_for_status()
            result = response.json()
            if isinstance(result, dict) and result.get("code") not in (0, None):
                raise RuntimeError(
                    f"Feishu rejected message: {result.get('code')} {result.get('msg')}"
                )

    def _build_pending_summary_message(
        self,
        summary,
        *,
        title: str,
        sender: str,
        current_node: str,
        deadline_text: str,
        detail_url: str,
        content_mode: str = "summary",
        max_summary_chars: int = 500,
        max_action_chars: int = 200,
        max_risk_items: int = 3,
        include_detail_link: bool = True,
    ) -> dict:
        """Build an interactive card for a single Pending summary.

        Only high-signal fields are sent: no full body or attachment content.
        ``content_mode`` controls how much is included (plan-0805-02 §3.5):

        * ``metadata_only`` — only title/sender/current node/deadline/link.
        * ``summary``       — also include a capped summary, required action,
          and at most ``max_risk_items`` risks.
        """

        def md(text) -> str:
            return self._redact(text) if isinstance(text, str) and text else ""

        def cap(value: str, limit: int) -> str:
            if value and len(value) > limit:
                return value[: max(0, limit - 1)] + "…"
            return value

        lines = [
            f"**标题**：{md(title)}",
            f"**发起人**：{md(sender)}",
        ]
        if current_node:
            lines.append(f"**当前节点**：{md(current_node)}")
        if content_mode != "metadata_only":
            summary_text = cap(md(summary.summary), max_summary_chars)
            if summary_text:
                lines.append(f"**简要说明**：{summary_text}")
            action_text = cap(md(summary.required_action), max_action_chars)
            if action_text:
                lines.append(f"**需要处理**：{action_text}")
            deadline = deadline_text or (summary.deadlines[0].date if summary.deadlines else "")
            if deadline:
                lines.append(f"**截止时间**：{md(deadline)}")
            risks = "\n".join(f"- {md(r.risk)}" for r in summary.risks[:max_risk_items] if r.risk)
            if risks:
                lines.append(f"**主要风险**：\n{risks}")
        else:
            deadline = deadline_text or (summary.deadlines[0].date if summary.deadlines else "")
            if deadline:
                lines.append(f"**截止时间**：{md(deadline)}")
        content = "\n".join(lines)
        elements = [{"tag": "div", "text": {"content_type": "markdown", "content": content}}]
        if include_detail_link and detail_url:
            elements.append({
                "tag": "div",
                "text": {"content_type": "markdown", "content": f"[查看 OA 详情]({detail_url})"},
            })
        header_title = md(title)[:20] or "待办摘要"
        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": f"待办摘要｜{header_title}"},
                    "template": "blue",
                },
                "elements": elements,
            },
        }

    def send_pending_summary(
        self,
        summary,
        *,
        title: str,
        sender: str,
        current_node: str,
        deadline_text: str,
        detail_url: str,
    ) -> bool:
        """Send one Pending item's summary as a Feishu card. Returns True if delivered."""
        if not self.webhook_url:
            logger.info("Feishu webhook not configured; skipping pending summary")
            return False

        message = self._build_pending_summary_message(
            summary, title=title, sender=sender,
            current_node=current_node, deadline_text=deadline_text, detail_url=detail_url,
        )
        for attempt in range(self.retry_attempts):
            try:
                self._post(message, timeout=30)
                return True
            except (httpx.RequestError, httpx.HTTPStatusError, RuntimeError) as exc:
                logger.warning("Feishu pending summary attempt %d failed: %s", attempt + 1, exc)
                if attempt < self.retry_attempts - 1:
                    time.sleep(2 ** attempt)
        logger.error("Feishu pending summary failed after %d attempts", self.retry_attempts)
        return False

    def send_digest(self, date_str: str, stats: dict) -> bool:
        """Send the daily digest to Feishu. Returns True if sent successfully."""
        if not self.webhook_url:
            logger.info("Feishu webhook not configured; skipping digest")
            return False

        message = self._build_digest_message(date_str, stats)

        for attempt in range(self.retry_attempts):
            try:
                self._post(message, timeout=30)
                logger.info("Feishu digest sent successfully: %s", date_str)
                return True
            except (httpx.RequestError, httpx.HTTPStatusError, RuntimeError) as exc:
                logger.warning("Feishu digest attempt %d failed: %s", attempt + 1, exc)
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

        try:
            self._post(payload, timeout=10)
            return True
        except (httpx.RequestError, httpx.HTTPStatusError, RuntimeError) as exc:
            logger.error("Feishu alert failed: %s", exc)
            return False
