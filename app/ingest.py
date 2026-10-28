"""Turn policy markdown files into section-level chunks with access metadata.

Each `###` subsection becomes one chunk. Its paragraphs and bullet items are
kept as separate blocks so Claude can cite a single bullet instead of a whole
section.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

CORPUS_DIR = Path(__file__).resolve().parent.parent / "corpus"

# Endorsements refer to the base form as "HO-3 Section 2.5"; we use that to
# pull an endorsement in whenever the section it amends is retrieved.
SECTION_REF = re.compile(r"HO-3(?: form)?\s+Section\s+(\d+\.\d+)", re.IGNORECASE)


@dataclass
class Chunk:
    chunk_id: str          # e.g. "HO3 §2.5"
    doc_id: str
    doc_title: str
    section: str           # e.g. "2.5"
    heading: str           # e.g. "Coverage D - Loss of Use (Additional Living Expense)"
    parent_heading: str    # e.g. "2. Property Coverages"
    blocks: List[str]
    jurisdiction: str      # "ALL" or a state code
    roles: List[str]
    version: str
    line: str
    amends: List[str] = field(default_factory=list)  # base chunk ids this chunk modifies

    @property
    def text(self) -> str:
        return "\n".join(self.blocks)

    @property
    def title(self) -> str:
        return f"{self.doc_title} — §{self.section} {self.heading}"

    @property
    def is_internal(self) -> bool:
        return "policyholder" not in self.roles


def parse_front_matter(raw: str):
    if not raw.startswith("---"):
        raise ValueError("missing front matter")
    _, fm, body = raw.split("---", 2)
    meta: Dict[str, object] = {}
    for line in fm.strip().splitlines():
        key, _, value = line.partition(":")
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            meta[key.strip()] = [v.strip() for v in value[1:-1].split(",") if v.strip()]
        else:
            meta[key.strip()] = value
    return meta, body


def _split_blocks(lines: List[str]) -> List[str]:
    """Paragraphs and individual bullets become separate citable blocks."""
    blocks: List[str] = []
    para: List[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if para:
                blocks.append(" ".join(para))
                para = []
        elif stripped.startswith("- "):
            if para:
                blocks.append(" ".join(para))
                para = []
            blocks.append(stripped)
        else:
            para.append(stripped)
    if para:
        blocks.append(" ".join(para))
    return blocks


def chunk_document(path: Path) -> List[Chunk]:
    meta, body = parse_front_matter(path.read_text(encoding="utf-8"))
    doc_id = str(meta["doc_id"])
    chunks: List[Chunk] = []
    parent = ""
    current = None  # (section, heading)
    buf: List[str] = []

    def flush():
        if current is None:
            return
        section, heading = current
        blocks = _split_blocks(buf)
        if not blocks:
            return
        amends = sorted({f"HO3 §{m}" for b in blocks for m in SECTION_REF.findall(b)})
        if meta["jurisdiction"] == "ALL":
            amends = []  # only state endorsements amend the base form
        chunks.append(Chunk(
            chunk_id=f"{doc_id} §{section}",
            doc_id=doc_id,
            doc_title=str(meta["title"]),
            section=section,
            heading=heading,
            parent_heading=parent,
            blocks=blocks,
            jurisdiction=str(meta["jurisdiction"]),
            roles=list(meta["roles"]),  # type: ignore[arg-type]
            version=str(meta["version"]),
            line=str(meta["line"]),
            amends=amends,
        ))

    for line in body.splitlines():
        if line.startswith("### "):
            flush()
            num, _, heading = line[4:].partition(" ")
            current = (num.strip(), heading.strip())
            buf = []
        elif line.startswith("## "):
            flush()
            current = None
            parent = line[3:].strip()
            buf = []
        elif line.startswith("# "):
            continue
        elif current is not None:
            buf.append(line)
    flush()
    return chunks


def load_corpus(corpus_dir: Path = CORPUS_DIR) -> List[Chunk]:
    chunks: List[Chunk] = []
    for path in sorted(corpus_dir.glob("*.md")):
        chunks.extend(chunk_document(path))
    ids = [c.chunk_id for c in chunks]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate chunk ids: {sorted(dupes)}")
    return chunks
