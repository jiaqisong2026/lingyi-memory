# -*- coding: utf-8 -*-
from __future__ import annotations
import logging, time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)
DEFAULT_TOP_K = 5
DEFAULT_TIMEOUT_MS = 3000


def perform_auto_recall(memory_manager, query, top_k=5, timeout_ms=3000):
    results: List[Dict[str, Any]] = []
    start = time.time()
    try:
        rows = memory_manager.search_memories(query, top_k=top_k)
        if not rows:
            return results
        for row in rows:
            if isinstance(row, dict):
                results.append(row)
            elif hasattr(row, "content"):
                results.append({
                    "id": getattr(row, "id", ""),
                    "content": getattr(row, "content", str(row)),
                    "importance": getattr(row, "importance", 0.0),
                    "level": getattr(row, "level", ""),
                    "category": getattr(row, "category", ""),
                })
            else:
                results.append({"content": str(row)})
    except Exception as e:
        logger.debug("perform_auto_recall error: %s", e)
    elapsed = (time.time() - start) * 1000
    logger.info("auto_recall query=%r hits=%d ms=%.1f", query, len(results), elapsed)
    return results


def format_recall_for_context(hits: List[Dict[str, Any]], max_chars: int = 2000) -> str:
    if not hits:
        return ""
    lines: List[str] = ["## Prior Relevant Experiences", ""]
    total = 0
    for i, hit in enumerate(hits, 1):
        content = hit.get("content", "")
        level = hit.get("level", "unknown")
        importance = hit.get("importance", 0.0)
        score = hit.get("score", 0.0)
        category = hit.get("category", "")
        if len(content) > 400:
            content = content[:400] + "..."
        tag = f"[{level}]" if level != "unknown" else ""
        lines.append(f"{i}. {tag} importance={importance:.2f}, score={score:.2f}, category={category}")
        lines.append("   " + content)
        total += len(content)
        if total > max_chars:
            break
    return chr(10).join(lines)

