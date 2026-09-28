# -*- coding: utf-8 -*-
"""Chinese-aware tokenization for the v2 memory store.

Why this exists:
- SQLite FTS5 ships with a `unicode61` tokenizer that splits on
  Unicode categories. It treats Chinese text as one long token,
  which kills recall quality on Chinese queries.
- The traditional workaround is external segmentation. We ship a
  small dependency-free segmenter that handles the common case
  (CJK characters split into 1-2 char windows + ascii tokens).
- This is **not** a replacement for jieba / pkuseg in production.
  It is the "good enough" path that lets this package run with
  zero non-stdlib dependencies. If a deployment has jieba, see
  `set_external_tokenizer` below to plug it in.

The window-based approach is the same trick that Elasticsearch's
`cjk` analyzer and Lucene'``s `CJKAnalyzer` use by default.
"""

from __future__ import annotations
import re
from typing import Callable, Iterable, List, Optional


# Match runs of CJK characters (basic block + extension A).
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]+")


def _bigram_windows(text: str) -> List[str]:
    """Produce overlapping 1- and 2-character windows over CJK runs."""
    out: List[str] = []
    for run in _CJK_RE.findall(text):
        n = len(run)
        if n == 0:
            continue
        out.append(run)  # the full run, in case recall needs it whole
        if n >= 2:
            for i in range(n - 1):
                out.append(run[i : i + 2])
    return out


def _ascii_tokens(text: str) -> List[str]:
    """Lower-case ascii word tokens, splitting on non-alphanumeric."""
    return [t.lower() for t in re.split(r"[^A-Za-z0-9_]+", text) if t]


# The default Chinese-aware tokenizer combines the two streams.
def cjk_ascii_tokenize(text: str) -> List[str]:
    """Tokenize a string into a list of recall-friendly tokens.

    The output preserves both CJK single chars and bigrams, plus
    lower-cased ascii words. Order is preserved so the indexer
    can detect "term X appeared before Y" if it wants to.
    """
    if not text:
        return []
    out: List[str] = []
    out.extend(_ascii_tokens(text))
    out.extend(_bigram_windows(text))
    return out


# External pluggable tokenizer (for production deployments that
# install jieba). The tokenize() function below always consults
# this first if set.
_external_tokenizer: Optional[Callable[[str], List[str]]] = None


def set_external_tokenizer(fn: Optional[Callable[[str], List[str]]]) -> None:
    """Plug in an external tokenizer (jieba, pkuseg, ...).

    Pass None to clear.

    Example with jieba::

        import jieba
        from src.kylin_v11_agent.text_tokenize import set_external_tokenizer

        def jieba_cut(text):
            return [w for w in jieba.cut(text) if w.strip()]

        set_external_tokenizer(jieba_cut)
    """
    global _external_tokenizer
    _external_tokenizer = fn


def tokenize(text: str) -> List[str]:
    """Return the active tokenizer'``s view of `text`."""
    if _external_tokenizer is not None:
        try:
            return _external_tokenizer(text)
        except Exception:
            # An external tokenizer is allowed to fail; we fall back
            # to the built-in one rather than crashing the indexer.
            pass
    return cjk_ascii_tokenize(text)


def tokenize_for_index(text: str) -> str:
    """Tokenize `text` and join with spaces for FTS5 indexing.

    SQLite FTS5'``s default tokenizer would otherwise see the raw
    unicode61 split, which on Chinese is one giant run. By
    pre-tokenizing and re-joining with spaces we force FTS5 to
    treat each token as a discrete indexable term.
    """
    return " ".join(tokenize(text))