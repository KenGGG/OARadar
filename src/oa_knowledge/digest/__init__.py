"""Digest package — task extraction and Feishu notifications."""

from oa_knowledge.digest.feishu import FeishuNotifier
from oa_knowledge.digest.tasks import TaskExtractor

__all__ = ["TaskExtractor", "FeishuNotifier"]
