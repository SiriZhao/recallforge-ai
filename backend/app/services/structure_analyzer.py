from __future__ import annotations

import hashlib
import re
from collections import Counter
from pathlib import Path

from app.schemas.review import DocumentBlock, DocumentSection, DocumentStructure, ParsedPage
from app.services.chapter_extractor import detect_chapter_title


def analyze_document_structure(
    filename: str,
    file_type: str,
    pages: list[ParsedPage],
    warnings: list[str] | None = None,
) -> DocumentStructure:
    """Build a stable, source-anchored document structure without dropping pages."""
    document_hash = hashlib.sha256()
    document_hash.update(filename.encode("utf-8", errors="ignore"))
    for page in pages:
        document_hash.update(page.text.encode("utf-8", errors="ignore"))

    blocks: list[DocumentBlock] = []
    heading_events: list[tuple[int, int, str, str]] = []
    for page in pages:
        anchor = source_anchor(filename, file_type, page.page_number)
        page.source_anchor = anchor
        if page.warning:
            page.status = "processed_with_warning" if page.text.strip() else "skipped_with_reason"
            page.confidence = 0.65 if page.text.strip() else 0.0
        elif not page.text.strip():
            page.status = "skipped_with_reason"
            page.warning = "该页没有可提取文本，已保留页面位置。"
            page.confidence = 0.0
        elif page.source == "ocr_fallback":
            page.confidence = min(page.confidence, 0.78)

        page_blocks = list(page.blocks)
        next_index = len(page_blocks) + 1
        headings = detect_headings(page.text)
        if page.title and not any(title == page.title for _, title in headings):
            headings.insert(0, (1, page.title))
        page.title = headings[0][1] if headings else page.title
        for level, title in headings:
            block_id = f"p{page.page_number}-b{next_index}"
            next_index += 1
            title_block = DocumentBlock(
                block_id=block_id,
                type="title",
                page_number=page.page_number,
                source_anchor=anchor,
                text=title,
                confidence=page.confidence,
                metadata={"level": level},
            )
            page_blocks.append(title_block)
            heading_events.append((page.page_number, level, title, block_id))

        if page.text.strip():
            page_blocks.append(
                DocumentBlock(
                    block_id=f"p{page.page_number}-b{next_index}",
                    type="text",
                    page_number=page.page_number,
                    source_anchor=anchor,
                    text=page.text.strip(),
                    confidence=page.confidence,
                )
            )
            next_index += 1

        for rows in detect_tables(page.text):
            page_blocks.append(
                DocumentBlock(
                    block_id=f"p{page.page_number}-b{next_index}",
                    type="table",
                    page_number=page.page_number,
                    source_anchor=anchor,
                    rows=rows,
                    confidence=page.confidence,
                    metadata={"detected_from": "text_layout"},
                )
            )
            next_index += 1

        for formula in detect_formulas(page.text):
            page_blocks.append(
                DocumentBlock(
                    block_id=f"p{page.page_number}-b{next_index}",
                    type="formula",
                    page_number=page.page_number,
                    source_anchor=anchor,
                    text=formula,
                    confidence=page.confidence,
                )
            )
            next_index += 1

        page.blocks = _dedupe_blocks(page_blocks)
        if any(block.type == "image" for block in page.blocks):
            note = "VISUAL_CONTENT_NOT_INTERPRETED: image preserved; chart/diagram semantics require review or explicit vision parsing."
            page.warning = f"{page.warning}; {note}" if page.warning else note
            page.status = "processed_with_warning"
        blocks.extend(page.blocks)

    recurring = Counter(title.casefold() for _, _, title, _ in heading_events)
    repeated_titles = {title for title, count in recurring.items() if len(pages) >= 4 and count >= max(3, len(pages) // 2)}
    heading_events = [event for event in heading_events if event[2].casefold() not in repeated_titles]
    title = heading_events[0][2] if heading_events else Path(filename).stem
    sections = build_section_tree(filename, file_type, pages, heading_events, blocks, title)
    structure_warnings = list(warnings or [])
    if len(pages) >= 40 and len(sections) <= 1:
        structure_warnings.append("STRUCTURE_ANOMALY_LONG_DOCUMENT_SINGLE_SECTION")
        sections = recover_long_document_sections(filename, file_type, pages, blocks)
    return DocumentStructure(
        document_id=document_hash.hexdigest()[:16],
        filename=filename,
        file_type=file_type,
        title=title,
        page_count=len(pages),
        sections=sections,
        blocks=blocks,
        warnings=structure_warnings,
    )


def recover_long_document_sections(filename: str, file_type: str, pages: list[ParsedPage], blocks: list[DocumentBlock]) -> list[DocumentSection]:
    windows=[]; start=1; window=8 if len(pages)<100 else 10
    for i,page in enumerate(pages,1):
        title=(page.title or "").strip()
        if i > 1 and title == (pages[i-2].title or "").strip():
            title = ""
        if title and i>start and len(title)<120: windows.append((start,i-1,f"Processing pages {start}–{i-1}")); start=i
        elif i-start+1>=window: windows.append((start,i,f"Processing pages {start}–{i}")); start=i+1
    if start<=len(pages): windows.append((start,len(pages),f"Processing pages {start}–{len(pages)}"))
    return [DocumentSection(section_id=f"recovered-section-{i}",title=t,level=1,page_start=lo,page_end=hi,block_ids=[b.block_id for b in blocks if lo<=b.page_number<=hi],source_anchor=source_anchor(filename,file_type,lo),basis="processing_window") for i,(lo,hi,t) in enumerate(windows,1)]

def source_anchor(filename: str, file_type: str, page_number: int) -> str:
    unit = "slide" if file_type.lower() == ".pptx" else "p."
    return f"{filename}, {unit} {page_number}"


def detect_headings(text: str) -> list[tuple[int, str]]:
    headings: list[tuple[int, str]] = []
    seen: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        markdown = re.match(r"^(#{1,6})\s+(.+?)\s*$", stripped)
        if markdown:
            level, title = len(markdown.group(1)), markdown.group(2).strip()
        else:
            title = detect_chapter_title(stripped)
            if not title:
                continue
            numbering = re.match(r"^(\d+(?:\.\d+)*)", stripped)
            level = min(6, numbering.group(1).count(".") + 1) if numbering else 1
        key = title.casefold()
        if key not in seen:
            headings.append((level, title))
            seen.add(key)
    return headings


def detect_tables(text: str) -> list[list[list[str]]]:
    tables: list[list[list[str]]] = []
    current: list[list[str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        delimiter = "|" if stripped.count("|") >= 2 else "\t" if "\t" in stripped else None
        if delimiter:
            cells = [cell.strip() for cell in stripped.strip("|").split(delimiter)]
            if len(cells) >= 2 and not all(re.fullmatch(r":?-{2,}:?", cell or "-") for cell in cells):
                current.append(cells)
                continue
        if len(current) >= 2:
            tables.append(current)
        current = []
    if len(current) >= 2:
        tables.append(current)
    return tables


def detect_formulas(text: str) -> list[str]:
    formulas: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if 3 <= len(stripped) <= 180 and re.search(r"[=∑∫√≈≤≥±∞]|\\frac|\\sum|P\(|E\(|Var\(", stripped):
            if stripped not in formulas:
                formulas.append(stripped)
    return formulas[:40]


def build_section_tree(
    filename: str,
    file_type: str,
    pages: list[ParsedPage],
    events: list[tuple[int, int, str, str]],
    blocks: list[DocumentBlock],
    fallback_title: str,
) -> list[DocumentSection]:
    if not pages:
        return []
    if not events:
        return [
            DocumentSection(
                section_id="section-1",
                title=fallback_title,
                page_start=1,
                page_end=len(pages),
                block_ids=[block.block_id for block in blocks],
                source_anchor=source_anchor(filename, file_type, 1),
            )
        ]

    sections: list[DocumentSection] = []
    stack: list[tuple[int, str]] = []
    for index, (page_number, level, title, _block_id) in enumerate(events):
        while stack and stack[-1][0] >= level:
            stack.pop()
        parent_id = stack[-1][1] if stack else None
        section_id = f"section-{index + 1}"
        next_page = events[index + 1][0] if index + 1 < len(events) else len(pages)
        page_end = max(page_number, next_page if index + 1 == len(events) else next_page - 1)
        section = DocumentSection(
            section_id=section_id,
            title=title,
            level=level,
            parent_id=parent_id,
            page_start=page_number,
            page_end=page_end,
            block_ids=[b.block_id for b in blocks if page_number <= b.page_number <= page_end],
            source_anchor=source_anchor(filename, file_type, page_number),
        )
        sections.append(section)
        if parent_id:
            parent = next(item for item in sections if item.section_id == parent_id)
            parent.child_ids.append(section_id)
        stack.append((level, section_id))
    return sections


def _dedupe_blocks(blocks: list[DocumentBlock]) -> list[DocumentBlock]:
    output: list[DocumentBlock] = []
    seen: set[tuple[str, str, str]] = set()
    for block in blocks:
        key = (block.type, block.text.strip().casefold(), repr(block.rows))
        if key in seen:
            continue
        seen.add(key)
        output.append(block)
    return output
