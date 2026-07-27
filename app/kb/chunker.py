"""
Semantic-ish text chunker.

Strategy:
1. Split on blank-line paragraph boundaries.
2. Pack paragraphs greedily up to max_tokens; emit a chunk when adding the next
   paragraph would exceed the cap.
3. If a single paragraph is longer than max_tokens, soft-split on sentence-ish
   delimiters ("。", "！", "？", ".", "!", "?", "\n") and fall back to fixed
   token windows with overlap when still too long.
"""
from __future__ import annotations

import re

import tiktoken

_SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?\.])\s+|(?<=[。！？!?])|\n+")

_ENCODER_CACHE: dict[str, tiktoken.Encoding] = {}


def _get_encoder(name: str) -> tiktoken.Encoding:
    enc = _ENCODER_CACHE.get(name)
    if enc is None:
        enc = tiktoken.get_encoding(name)
        _ENCODER_CACHE[name] = enc
    return enc


def count_tokens(text: str, tokenizer_name: str = "cl100k_base") -> int:
    if not text:
        return 0
    enc = _get_encoder(tokenizer_name)
    return len(enc.encode(text))


def _split_oversized(
    text: str,
    enc: tiktoken.Encoding,
    max_tokens: int,
    overlap_tokens: int,
) -> list[str]:
    """Split a too-large fragment. Prefer sentence boundaries, then token windows."""
    sents = [s.strip() for s in _SENT_SPLIT_RE.split(text) if s and s.strip()]
    out: list[str] = []
    buf: list[str] = []
    buf_tok = 0
    for s in sents:
        n = len(enc.encode(s))
        if n >= max_tokens:
            # flush buffer first
            if buf:
                out.append(" ".join(buf).strip())
                buf = []
                buf_tok = 0
            # window-slice this giant sentence
            tokens = enc.encode(s)
            step = max(1, max_tokens - overlap_tokens)
            for i in range(0, len(tokens), step):
                window = tokens[i : i + max_tokens]
                if not window:
                    break
                out.append(enc.decode(window).strip())
                if i + max_tokens >= len(tokens):
                    break
            continue
        if buf_tok + n > max_tokens:
            out.append(" ".join(buf).strip())
            buf = [s]
            buf_tok = n
        else:
            buf.append(s)
            buf_tok += n
    if buf:
        out.append(" ".join(buf).strip())
    return [c for c in out if c]


def chunk_text(
    text: str,
    max_tokens: int = 400,
    overlap_tokens: int = 60,
    tokenizer_name: str = "cl100k_base",
) -> list[str]:
    """Chunk text into <=max_tokens pieces, splitting on paragraphs then sentences."""
    if not text or not text.strip():
        return []
    enc = _get_encoder(tokenizer_name)
    # Split into paragraphs (blank-line separated).
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p and p.strip()]
    chunks: list[str] = []
    buf: list[str] = []
    buf_tok = 0

    def flush(*, carry_overlap: bool = False) -> None:
        nonlocal buf, buf_tok
        if buf:
            emitted = "\n\n".join(buf).strip()
            chunks.append(emitted)
            if carry_overlap and overlap_tokens > 0:
                tail = enc.encode(emitted)[-min(overlap_tokens, max_tokens - 1) :]
                overlap = enc.decode(tail).strip()
                buf = [overlap] if overlap else []
                buf_tok = len(enc.encode(overlap)) if overlap else 0
            else:
                buf = []
                buf_tok = 0

    for para in paragraphs:
        n = len(enc.encode(para))
        if n > max_tokens:
            flush(carry_overlap=False)
            chunks.extend(_split_oversized(para, enc, max_tokens, overlap_tokens))
            continue
        if buf_tok + n > max_tokens:
            flush(carry_overlap=True)
            # A large next paragraph may leave insufficient room for the full
            # overlap tail. Keep the newest portion that still fits.
            if buf_tok + n > max_tokens and buf:
                available = max(0, max_tokens - n)
                tail = enc.encode(buf[0])[-available:] if available else []
                overlap = enc.decode(tail).strip() if tail else ""
                buf = [overlap] if overlap else []
                buf_tok = len(tail)
        buf.append(para)
        buf_tok += n
    flush(carry_overlap=False)
    return [c for c in chunks if c]
