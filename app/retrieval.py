"""Access-filtered BM25 retrieval with insurance query expansion and endorsement linking.

Order of operations matters for compliance: chunks the caller may not see are
removed *before* scoring, so a restricted chunk can never surface, be sent to
the model, or leak through a citation.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

from .ingest import Chunk

STOPWORDS = set("""
a an and are as at be by can do does for from has have how i if in is it its my of on or our
so that the their them then there these this to under up was we what when where which who
will with would you your me am get got any all not no
""".split())

# Policyholders don't talk like policy forms. Each key expands to the policy's
# own vocabulary. Built from the golden-set misses; extend it the same way.
SYNONYMS: Dict[str, str] = {
    "ale": "additional living expense loss of use",
    "hotel": "additional living expense loss of use",
    "displaced": "additional living expense loss of use uninhabitable",
    "comprehensive": "other than collision",
    "uber": "transportation network delivery",
    "lyft": "transportation network delivery",
    "doordash": "transportation network delivery",
    "rideshare": "transportation network delivery",
    "mold": "fungi rot bacteria",
    "mildew": "fungi rot bacteria",
    "earthquake": "earth movement earthquake",
    "sinkhole": "earth movement",
    "sewer": "water backs up sewers drains",
    "jewelry": "jewelry theft special limits",
    "rental": "rental reimbursement transportation expenses",
    "stolen": "theft",
    "wildfire": "state of emergency fire total loss",
    "deductible": "deductible",
    "lawsuit": "suit against us action",
    "sue": "suit against us action",
    "reserve": "reserve authority",
    "fraud": "special investigations unit fraud indicators",
    "siu": "special investigations unit",
    "roof": "roof surfacing",
    "tree": "trees felled debris removal",
    "hit-and-run": "hit-and-run uninsured motor vehicle",
    "cash": "money bank notes special limits",
    "leak": "continuous repeated seepage leakage plumbing",
    "slowly": "continuous repeated seepage",
    "disagree": "appraisal fail to agree amount of loss",
    "dispute": "appraisal fail to agree amount of loss",
    "estimate": "amount of loss appraisal",
    "froze": "freezing plumbing system vacant unoccupied heat",
    "frozen": "freezing plumbing system vacant unoccupied heat",
    "itemize": "itemized inventory",
}

# Multi-word phrases checked against the raw query before tokenizing.
PHRASES: Dict[str, str] = {
    "drove off": "hit-and-run uninsured motor vehicle",
    "drove away": "hit-and-run uninsured motor vehicle",
    "fled the scene": "hit-and-run uninsured motor vehicle",
    "under my sink": "plumbing system seepage leakage",
    "burns down": "total loss fire",
    "burned down": "total loss fire",
    "every item": "itemized inventory",
}


def _stem(tok: str) -> str:
    if len(tok) <= 4:
        return tok
    if tok.endswith("ies"):
        return tok[:-3] + "y"
    if tok.endswith(("sses", "xes", "zes", "ches", "shes")):
        return tok[:-2]
    for suffix in ("ing", "ed", "s"):
        if len(tok) > len(suffix) + 2 and tok.endswith(suffix) and not tok.endswith("ss"):
            return tok[: -len(suffix)]
    return tok


def tokenize(text: str) -> List[str]:
    toks = re.findall(r"[a-z0-9][a-z0-9\-\.%$]*", text.lower())
    return [_stem(t.strip(".-")) for t in toks if t.strip(".-") and t not in STOPWORDS]


def expand_query(query: str) -> str:
    q = query.lower()
    extra = [v for k, v in PHRASES.items() if k in q]
    for t in re.findall(r"[a-z\-]+", q):
        key = t if t in SYNONYMS else _stem(t)  # "earthquakes" -> "earthquake", "leaking" -> "leak"
        if key in SYNONYMS:
            extra.append(SYNONYMS[key])
    return query + (" " + " ".join(extra) if extra else "")


def can_access(chunk: Chunk, role: str, state: Optional[str]) -> bool:
    if role not in chunk.roles:
        return False
    return chunk.jurisdiction == "ALL" or (state is not None and chunk.jurisdiction == state)


@dataclass
class Hit:
    chunk: Chunk
    score: float
    reason: str  # "bm25" or "endorsement-link"


class BM25Index:
    def __init__(self, chunks: Iterable[Chunk], k1: float = 1.4, b: float = 0.75):
        self.chunks = list(chunks)
        self.k1, self.b = k1, b
        self.docs = [self._index_text(c) for c in self.chunks]
        self.tf = [Counter(d) for d in self.docs]
        self.avgdl = sum(len(d) for d in self.docs) / max(len(self.docs), 1)
        df: Counter = Counter()
        for d in self.docs:
            df.update(set(d))
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.by_id = {c.chunk_id: c for c in self.chunks}

    @staticmethod
    def _index_text(c: Chunk) -> List[str]:
        # Headings are weighted twice: "Coverage D - Loss of Use" is the strongest signal.
        return tokenize(f"{c.heading} {c.heading} {c.parent_heading} {c.doc_title} {c.text}")

    def score(self, query_toks: List[str], i: int) -> float:
        tf, dl = self.tf[i], len(self.docs[i])
        s = 0.0
        for t in set(query_toks):
            if t not in tf:
                continue
            f = tf[t]
            s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
        return s


class Retriever:
    def __init__(self, chunks: List[Chunk]):
        self.index = BM25Index(chunks)
        # base chunk id -> endorsement chunks that amend it
        self.amended_by: Dict[str, List[Chunk]] = {}
        for c in chunks:
            for target in c.amends:
                self.amended_by.setdefault(target, []).append(c)

    def search(self, query: str, role: str, state: Optional[str], k: int = 6) -> List[Hit]:
        qtoks = tokenize(expand_query(query))
        scored = []
        for i, c in enumerate(self.index.chunks):
            if not can_access(c, role, state):
                continue
            s = self.index.score(qtoks, i)
            if s > 0:
                scored.append(Hit(c, s, "bm25"))
        scored.sort(key=lambda h: h.score, reverse=True)
        hits = scored[:k]

        # A state endorsement changes what the base form means. If we retrieved
        # the base section, the amending endorsement must ride along with it.
        seen = {h.chunk.chunk_id for h in hits}
        linked: List[Hit] = []
        for h in hits:
            for e in self.amended_by.get(h.chunk.chunk_id, []):
                if e.chunk_id not in seen and can_access(e, role, state):
                    linked.append(Hit(e, h.score, "endorsement-link"))
                    seen.add(e.chunk_id)
        return hits + linked
