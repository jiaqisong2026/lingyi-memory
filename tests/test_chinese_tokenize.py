# -*- coding: utf-8 -*-
"""Tests for the CJK+ASCII tokenizer used by the v2 memory indexer."""

from __future__ import annotations
import os, sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
sys.path = [p for p in sys.path if "银河os" not in p]

from src.kylin_v11_agent.text_tokenize import (
    cjk_ascii_tokenize, tokenize, tokenize_for_index,
    set_external_tokenizer, _bigram_windows,
)


class TestBigramWindows:
    def test_short_chinese_run(self):
        # A 2-char run yields itself plus 1 bigram.
        toks = _bigram_windows("中文")
        assert toks == ["中文", "中文"]

    def test_longer_chinese_run(self):
        # "语言模型" -> 4-char run -> the run + 3 bigrams.
        toks = _bigram_windows("语言模型")
        assert "语言模型" in toks
        assert "语言" in toks
        assert "言模" in toks
        assert "模型" in toks
        assert len([t for t in toks if len(t) == 2]) == 3

    def test_mixed_ascii_and_cjk(self):
        toks = cjk_ascii_tokenize("opencode 模型 release v0.5")
        # ascii tokens lowercased
        assert "opencode" in toks
        assert "release" in toks
        assert "v0" in toks or "v0.5" in toks
        # CJK bigrams
        assert any(len(t) == 2 and all("\u4e00" <= c <= "\u9fff" for c in t) for t in toks)


class TestCjkAsciiTokenize:
    def test_empty_string(self):
        assert cjk_ascii_tokenize("") == []

    def test_ascii_only(self):
        toks = cjk_ascii_tokenize("hello world foo bar")
        assert toks == ["hello", "world", "foo", "bar"]

    def test_chinese_only(self):
        toks = cjk_ascii_tokenize("你好世界")
        # the full run + 3 bigrams.
        assert "你好世界" in toks
        assert "你好" in toks
        assert "好世" in toks
        assert "世界" in toks

    def test_numbers(self):
        toks = cjk_ascii_tokenize("订单12345")
        # The CJK part is segmented, the numeric part is a single token.
        assert "订单" in toks  # 2-char CJK run -> single bigram
        assert "12345" in toks  # numeric token
        assert any(t.isdigit() for t in toks)


class TestExternalTokenizerPlug:
    def teardown_method(self):
        # Always clear the global after the test.
        set_external_tokenizer(None)

    def test_external_tokenizer_takes_precedence(self):
        def my_tok(text: str):
            return text.split()

        set_external_tokenizer(my_tok)
        assert tokenize("a b c d") == ["a", "b", "c", "d"]

    def test_external_tokenizer_failure_falls_back(self):
        def bad_tok(text: str):
            raise RuntimeError("boom")

        set_external_tokenizer(bad_tok)
        # We do not raise; we fall back to the built-in tokenizer.
        toks = tokenize("中文")
        assert "中文" in toks

    def test_clearing_external_tokenizer(self):
        def my_tok(text: str):
            return ["only_this"]

        set_external_tokenizer(my_tok)
        assert tokenize("anything") == ["only_this"]
        set_external_tokenizer(None)
        assert "only_this" not in tokenize("anything")


class TestTokenizeForIndex:
    def test_space_separated_output(self):
        out = tokenize_for_index("hello 中文 world")
        # Space-separated tokens.
        assert "hello" in out.split()
        assert "中文" in out.split()
        # Big grams also present.
        assert any(len(t) == 2 for t in out.split())