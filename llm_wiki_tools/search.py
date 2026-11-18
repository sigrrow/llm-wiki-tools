"""질문에 맞는 절을 코드가 먼저 찾는다. 모델은 찾은 절만 받아 답한다 — grep 을 수십 번 반복하다 빈 답이 나던 경로가 없다.

요약 노트(notes/)의 절을 먼저 SUMMARY_CHARS 까지 담고, 원문 부분(sources/)에서 낱말이 맞은 곳 앞뒤를 잘라 채운다.
원문은 요약이 덮지 못한 낱말이 있는 곳부터 넣는다 — 요약에서 빠진 고유명사·수치도 찾게.

일치는 공백·하이픈·가운뎃점을 지우고 소문자로 바꾼 글자열끼리 본다 — 'GPU Live' 와 'GPU-Live',
'투라인 클라우드' 와 '투라인클라우드' 가 같은 말로 잡힌다.
"""
from __future__ import annotations

import re
from typing import Any

from . import store

RESULT_CHARS = 7_200
SUMMARY_CHARS = 4_800  # 원문 발췌가 있으면 요약 절은 여기까지 먼저 담고 나머지를 원문에 준다
SECTION_CHARS = 1_800
MAX_SECTIONS = 8
EXCERPT_CHARS = 1_500  # 원문 부분 하나에서 잘라 오는 양
WINDOW = 250  # 맞은 곳 앞뒤로 붙이는 글자 수
HITS_PER_TERM = 3

_PARTICLES = ("에서는", "에서", "으로", "이야", "인가", "인지", "에게", "까지", "부터", "처럼", "보다",
              "은", "는", "이", "가", "을", "를", "에", "의", "로", "와", "과", "도", "만", "야", "중")
_STOP = {"어디", "어떤", "무엇", "뭐", "누구", "언제", "어떻게", "왜", "알려줘", "알려", "줘", "있어", "있나", "인가요",
         "뭐야", "환경이야", "그", "이", "저", "좀"}
_NORM = re.compile(r"[\s\-_·.,/()\[\]]+")


def norm(text: str) -> str:
    return _NORM.sub("", text).lower()


def strip_particle(word: str) -> str:
    for particle in _PARTICLES:
        if len(word) > len(particle) + 1 and word.endswith(particle):
            return word[: -len(particle)]
    return word


def terms(question: str) -> list[str]:
    """낱말과 이웃한 두 낱말을 붙인 말을 함께 쓴다(띄어 쓴 고유명사 대비). 긴 말부터."""
    words = [strip_particle(w) for w in re.findall(r"[0-9A-Za-z가-힣]+", question)]
    words = [w for w in words if w not in _STOP and len(norm(w)) >= 2]
    found = set(norm(w) for w in words)
    found |= {norm(a + b) for a, b in zip(words, words[1:])}
    return sorted(found, key=lambda t: (-len(t), t))


def sections(markdown: str) -> list[str]:
    parts = re.split(r"(?m)^(?=## )", markdown)
    head = parts[0].strip()
    return [p.strip() for p in parts[1:]] or ([head] if head else [])


def _normalized(text: str) -> tuple[str, list[int]]:
    """norm() 과 같은 글자열과, 그 글자마다 원문에서의 위치."""
    chars, where = [], []
    for i, ch in enumerate(text):
        if _NORM.fullmatch(ch):
            continue
        for low in ch.lower():
            chars.append(low)
            where.append(i)
    return "".join(chars), where


def excerpt(text: str, keys: list[str]) -> str:
    """원문에서 낱말이 맞은 곳 앞뒤 WINDOW 자를 잘라 겹치면 잇는다. 끊긴 곳은 ' … ' 로 표시한다."""
    flat, where = _normalized(text)
    spans: list[tuple[int, int]] = []
    for key in keys:
        start = flat.find(key)
        for _ in range(HITS_PER_TERM):
            if start < 0:
                break
            begin, end = where[start], where[start + len(key) - 1] + 1
            spans.append((max(0, begin - WINDOW), min(len(text), end + WINDOW)))
            start = flat.find(key, start + len(key))
    merged: list[list[int]] = []
    for begin, end in sorted(spans):
        if merged and begin <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([begin, end])
    pieces = [("… " if begin > 0 else "") + text[begin:end].strip() + (" …" if end < len(text) else "")
              for begin, end in merged]
    return "\n".join(pieces)[:EXCERPT_CHARS]


def _summary_hits(space: str, keys: list[str]) -> list[tuple[int, str, str]]:
    root = store.notes_dir(space)
    hits: list[tuple[int, str, str]] = []
    for page in sorted(root.rglob("*.md")):
        if page.name == "index.md":
            continue
        rel = page.relative_to(root).as_posix()
        text = page.read_text(encoding="utf-8")
        title = text.splitlines()[0].lstrip("# ").strip() if text else rel
        for section in sections(text):
            score = sum(len(k) for k in keys if k in norm(section))
            if score:
                hits.append((score, rel, f"[{title} · notes/{rel}]\n{section[:SECTION_CHARS]}"))
    return sorted(hits, key=lambda h: (-h[0], h[1]))


def _source_hits(space: str, keys: list[str], covered: set[str]) -> list[tuple[int, int, str, str]]:
    """(요약이 못 덮은 낱말 점수, 전체 점수, 정렬 키, 발췌 블록). 문서 이름·부분 수는 state 에서 읽는다."""
    docs = store.load_state(space)["files"]
    hits = []
    for path in sorted(store.sources_dir(space).glob("*/*.txt")):
        meta = docs.get(path.parent.name)
        if not meta or not path.stem.isdigit():
            continue  # 뺀 문서의 남은 원문은 쓰지 않는다
        text = path.read_text(encoding="utf-8")
        found = [k for k in keys if k in norm(text)]
        if not found:
            continue
        fresh = sum(len(k) for k in found if k not in covered)
        label = f"[원문 · {meta.get('name', path.parent.name)} {path.stem}/{meta.get('parts', '?')} 부분]"
        hits.append((fresh, sum(len(k) for k in found), f"{path.parent.name}/{int(path.stem):03d}",
                     f"{label}\n{excerpt(text, found)}"))
    return sorted(hits, key=lambda h: (-h[0], -h[1], h[2]))


def search(space: str, question: str) -> dict[str, Any]:
    keys = terms(question)
    summaries = _summary_hits(space, keys)[:MAX_SECTIONS]
    picked: list[str] = []
    size = 0

    def take(block: str, limit: int) -> bool:
        nonlocal size
        if size + len(block) > limit:
            return False
        picked.append(block)
        size += len(block) + 2
        return True

    first = [b for _, _, b in summaries]
    rest: list[str] = []
    for i, block in enumerate(first):  # 요약 절을 먼저 SUMMARY_CHARS 까지
        if not take(block, SUMMARY_CHARS):
            rest = first[i:]
            break
    covered = {k for k in keys if any(k in norm(b) for b in picked)}
    sources = _source_hits(space, keys, covered)
    for _, _, _, block in sources:  # 원문 발췌로 RESULT_CHARS 까지
        take(block, RESULT_CHARS)
    for block in rest:  # 남은 자리는 요약 절로
        if not take(block, RESULT_CHARS):
            break
    matched = sorted({k for k in keys if any(k in norm(b) for b in picked)})
    text = "\n\n".join(picked) if picked else "(일치하는 절 없음)"
    return {"terms": keys, "matched_terms": matched, "hit_count": len(summaries) + len(sources), "context": text}
