from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SemanticChunk:
    chunk_id: str
    text: str
    section_title: str
    source_anchors: list[str] = field(default_factory=list)
    overlap_from_previous: str = ""


def split_semantic_chunks(
    text: str,
    *,
    max_chars: int,
    overlap_chars: int,
) -> list[SemanticChunk]:
    """Split on structural boundaries and carry a small labelled bridge between chunks."""
    if not text.strip():
        return []
    units = _structural_units(text)
    chunks: list[SemanticChunk] = []
    current: list[str] = []
    current_length = 0
    current_section = "未命名专题"
    anchors: list[str] = []
    last_source_anchors: list[str] = []

    def flush() -> None:
        nonlocal current, current_length, anchors
        body = "\n\n".join(current).strip()
        if not body:
            return
        bridge = _bridge_text(chunks[-1].text, overlap_chars) if chunks else ""
        prefix = f"[承接上文，仅用于上下文，不要重复总结]\n{bridge}\n\n" if bridge else ""
        if len(prefix) + len(body) > max_chars:
            prefix = ""
        chunks.append(
            SemanticChunk(
                chunk_id=f"chunk-{len(chunks) + 1}",
                text=prefix + body,
                section_title=current_section,
                source_anchors=list(dict.fromkeys(anchors)),
                overlap_from_previous=bridge,
            )
        )
        current = []
        current_length = 0
        anchors = []

    for unit in units:
        heading = _heading(unit)
        if heading:
            current_section = heading
        unit_anchors = re.findall(r"\[[^\]\n]+,\s*(?:p\.|slide)\s*\d+\]", unit)
        if unit_anchors:
            last_source_anchors = list(unit_anchors)
        else:
            unit_anchors = list(last_source_anchors)
        if len(unit) > max_chars:
            flush()
            position = 0
            while position < len(unit):
                overlap_start = max(0, position - overlap_chars) if position else 0
                bridge = unit[overlap_start:position] if position else ""
                prefix = (
                    f"[承接上文，仅用于上下文，不要重复总结]\n{bridge}\n\n"
                    if bridge
                    else ""
                )
                budget = max(1, max_chars - len(prefix))
                fragment = prefix + unit[position : position + budget]
                if fragment.strip():
                    chunks.append(
                        SemanticChunk(
                            chunk_id=f"chunk-{len(chunks) + 1}",
                            text=fragment,
                            section_title=current_section,
                            source_anchors=list(dict.fromkeys(unit_anchors)),
                            overlap_from_previous=bridge,
                        )
                    )
                position += budget
            continue
        projected = current_length + len(unit) + 2
        if current and projected > max_chars:
            flush()
        current.append(unit)
        current_length += len(unit) + 2
        anchors.extend(unit_anchors)
    flush()
    return chunks


def _structural_units(text: str) -> list[str]:
    units: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if _heading(line) and current:
            units.append("\n".join(current).strip())
            current = [line]
        elif not line.strip() and current:
            units.append("\n".join(current).strip())
            current = []
        else:
            current.append(line)
    if current:
        units.append("\n".join(current).strip())
    return [unit for unit in units if unit]


def _heading(text: str) -> str | None:
    first = text.strip().splitlines()[0] if text.strip() else ""
    match = re.match(r"^(?:#{1,6}\s+|第\s*[\d一二三四五六七八九十百]+[章节篇]\s*|(?:Chapter|Unit|Lecture)\s+\d+\s*)(.{2,80})$", first, re.I)
    if match:
        return re.sub(r"^#{1,6}\s+", "", first).strip()
    if re.match(r"^\d+(?:\.\d+){0,4}\s+\S.{1,70}$", first):
        return first.strip()
    return None


def _bridge_text(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    selected: list[str] = []
    length = 0
    for part in reversed(paragraphs):
        if length + len(part) + 2 > limit:
            break
        selected.append(part)
        length += len(part) + 2
    return "\n\n".join(reversed(selected))
