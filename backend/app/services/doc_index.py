"""A stub doc-search function for the assistant's context assembly.

**This is a placeholder, not the retrieval lane.** `vc/assistant-retrieval` owns the
real doc index (embeddings, a real ranking function, the actual chunking policy) and
had not merged as of this branch. Everything below is a deliberately simple,
dependency-free keyword-overlap search over the same markdown files a human would
read — good enough to return real citations (a doc name and a heading) for the demo,
not good enough to call "retrieval."

**Swap point:** `app/services/assistant.py` imports only `search` and `DocChunk` from
this module. Once `vc/assistant-retrieval` merges, replace this file's contents (or
this import) with the real module — nothing else in `assistant.py` needs to change,
as long as the real `search(query, k)` returns the same `list[DocChunk]` shape.

No LLM call, no network, no database — pure text over files already in the repo.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# backend/app/services/doc_index.py -> repo root -> docs/
_DOC_ROOT = Path(__file__).resolve().parents[3] / "docs"

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_WORD = re.compile(r"[a-z0-9]+")

# Every ADR except the empty template, plus the two docs a demo question is most
# likely to be answered from. Listed explicitly rather than globbed at request time,
# so the index is a fixed, reviewable set of sources rather than "whatever happens to
# be in docs/ today" (which would silently start citing an unrelated audit note).
_SOURCE_FILES: tuple[Path, ...] = (
    _DOC_ROOT / "SYSTEM-EXPLAINED.md",
    _DOC_ROOT / "CONTEXT.md",
    *sorted(p for p in (_DOC_ROOT / "adr").glob("*.md") if p.name != "0000-template.md"),
)


@dataclass(frozen=True, slots=True)
class DocChunk:
    """One retrievable unit: a heading and the text under it, from one doc.

    `doc` and `section` are exactly the two fields `AssistantSource` (the API's
    citation shape) needs — this dataclass is the seam that keeps context assembly
    and the wire format from drifting apart.
    """

    doc: str
    section: str
    text: str


def _doc_title(path: Path, first_line: str) -> str:
    """The name a citation shows for one source file.

    ADRs cite by number (`ADR-0006`, matching how `docs/SYSTEM-EXPLAINED.md` itself
    refers to them) rather than by their long filename slug — "cites ADR-0006 by
    name" is the whole point of returning sources at all.
    """
    match = re.match(r"^(\d{4})-", path.stem)
    if match:
        return f"ADR-{match.group(1)}"
    heading = _HEADING.match(first_line)
    return heading.group(2).strip() if heading else path.stem


def _chunks_for(path: Path) -> list[DocChunk]:
    """Split one markdown file into one chunk per heading (any level).

    A chunk's `text` is the heading line plus everything up to the next heading —
    coarser than a real retrieval index would use, but every chunk is still a
    coherent, human-authored unit (a glossary entry, an ADR's "Defend it" cell,
    a numbered flow step), not an arbitrary character window.
    """
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    doc = _doc_title(path, lines[0] if lines else "")

    chunks: list[DocChunk] = []
    section = "Overview"
    body: list[str] = []

    def _flush() -> None:
        text = "\n".join(body).strip()
        if text:
            chunks.append(DocChunk(doc=doc, section=section, text=text))

    for line in lines:
        heading = _HEADING.match(line)
        if heading:
            _flush()
            section = heading.group(2).strip()
            body = [line]
        else:
            body.append(line)
    _flush()
    return chunks


@lru_cache(maxsize=1)
def _all_chunks() -> tuple[DocChunk, ...]:
    """Every chunk from every source file, read and split once per process —
    these files don't change while the backend is running.
    """
    return tuple(chunk for path in _SOURCE_FILES for chunk in _chunks_for(path))


def _terms(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def search(query: str, k: int = 5) -> list[DocChunk]:
    """The top `k` chunks whose heading or text best overlaps `query`'s terms.

    Keyword overlap, not semantic similarity — see the module docstring. A term
    matched in the section heading counts for more than one matched in the body,
    on the theory that a heading naming the concept is stronger evidence of
    relevance than the word merely appearing somewhere in a paragraph. Chunks
    with zero overlap are dropped rather than padded in: an honest "nothing
    matched" is more useful to `assistant.py` than a citation that doesn't
    actually support the answer.
    """
    query_terms = _terms(query)
    if not query_terms:
        return []

    scored: list[tuple[float, int, DocChunk]] = []
    for index, chunk in enumerate(_all_chunks()):
        heading_hits = len(query_terms & _terms(chunk.section)) * 3
        doc_hits = len(query_terms & _terms(chunk.doc)) * 2
        body_hits = len(query_terms & _terms(chunk.text))
        score = heading_hits + doc_hits + body_hits
        if score > 0:
            # `index` as the tiebreaker keeps ties in file order, so the same
            # query always returns the same chunks in the same order.
            scored.append((score, index, chunk))

    scored.sort(key=lambda item: (-item[0], item[1]))
    return [chunk for _, _, chunk in scored[:k]]
