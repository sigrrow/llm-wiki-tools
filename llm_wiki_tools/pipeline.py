"""묶기 → 합치기 → 색인. 모델이 하는 일은 묶음 하나를 요약해 JSON 으로 돌려주는 것뿐이다.

모델이 도구 에이전트 턴으로 페이지 전체를 다시 쓰는 대신, 여기서는
  - 원문 읽기·자르기·묶기는 코드가, 페이지 합치기는 문서 부분(`## [Part N]`) 절 단위로 코드가 한다.
  - 모델은 바뀐 항목의 요약만 낸다 → 기존 항목이 사라지거나 바뀌는 일이 구조적으로 없다.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from typing import Any

from . import store

# 레시피 llm 노드 입력 하나의 상한이 8,000자다(recipes/io.ts MAX_JSON_INLINE_CHARS). 머리말 몫을 남긴다.
SLOT_CHARS = 7_200
SLOTS = 16  # 레시피 요약 llm 노드 수·wiki_merge 의 out_N 수와 같아야 한다(llm-wiki-synth.yaml n_sum_1~16)
SUMMARY_CHARS = 3_000


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " …(이하 생략)"


def today(now: float | None = None) -> str:
    return datetime.fromtimestamp(now if now is not None else time.time(), timezone.utc).strftime("%Y-%m-%d")


# ── 묶기 ─────────────────────────────────────────────────────────────────────
EMPTY_SLOT = "(비어 있음)"


def pack(items: list[dict[str, Any]], solo: set[str] = frozenset()) -> list[list[dict[str, Any]]]:
    """대기열 앞에서부터 순서대로 슬롯을 채운다. 같은 입력이면 같은 묶음이 나온다.
    solo(앞서 요약이 빠졌던 항목)는 슬롯을 혼자 쓴다 — 같은 묶음이 거듭 빈 답을 내면 함께 묶인 항목까지 잃는다."""
    slots: list[list[dict[str, Any]]] = [[] for _ in range(SLOTS)]
    sizes = [0] * SLOTS
    index = 0
    for item in items:
        size = len(item["text"]) + 2
        alone = item["id"] in solo
        while index < SLOTS and slots[index] and (alone or sizes[index] + size > SLOT_CHARS or slots[index][0]["id"] in solo):
            index += 1
        if index >= SLOTS:
            break
        slots[index].append(item)
        sizes[index] += size
    return slots


def runs_needed(items: list[dict[str, Any]]) -> int:
    """대기 항목을 모두 정리하려면 synth 를 몇 번 돌려야 하는지(실패 없이). next_batch 와 같은 pack 을 되풀이한다."""
    runs, rest = 0, list(items)
    while rest:
        taken = sum(len(slot) for slot in pack(rest))
        if not taken:  # 슬롯 하나에도 안 들어가는 항목은 없다(add 가 SLOT_CHARS 안으로 자른다) — 끝없이 돌지 않게 막는다
            break
        rest, runs = rest[taken:], runs + 1
    return runs


def render_slot(items: list[dict[str, Any]]) -> str:
    return "\n\n".join(item["text"] for item in items) if items else EMPTY_SLOT


def next_batch(space: str) -> dict[str, Any]:
    """앞 실행이 요약 도중 끊겼으면(inflight 가 남음) 같은 항목을 다시 낸다. 끊긴 실행은 항목 실패가 아니라 따로 센다."""
    if not store.load_config(space):
        raise ValueError(f"'{space}' 위키가 아직 없다 — llm-wiki-init 을 먼저 실행하라")
    state = store.load_state(space)
    queued = {item["id"]: item for item in store.list_queue(space)}
    quarantined: list[str] = []
    inflight = store.read_json(store.inflight_path(space), None)
    if inflight:
        for item_id in [i for slot in inflight["slots"] for i in slot]:
            state["interrupted"][item_id] = state["interrupted"].get(item_id, 0) + 1
            if state["interrupted"][item_id] >= store.MAX_INTERRUPTED:
                store.quarantine(space, item_id)
                queued.pop(item_id, None)
                quarantined.append(item_id)
        store.save_state(space, state)
    slots = pack(list(queued.values()), set(state["attempts"]) | set(state["blank"]))
    batch = {"slots": [[item["id"] for item in slot] for slot in slots]}
    if any(batch["slots"]):
        store.write_json(store.inflight_path(space), batch)
    else:
        store.inflight_path(space).unlink(missing_ok=True)
    taken = sum(len(slot) for slot in slots)
    result = {f"slot_{i + 1}": render_slot(slot) for i, slot in enumerate(slots)}
    result.update({"taken": taken, "left_after": len(queued) - taken, "quarantined": quarantined})
    return result


# ── 합치기 ───────────────────────────────────────────────────────────────────
_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


def _salvage(text: str) -> list[dict[str, Any]]:
    """전체 JSON 이 깨졌을 때(출력이 max_tokens 에서 잘림 등) 완전히 닫힌 {"id", "summary"} 객체만 하나씩 건진다."""
    decoder, found, i = json.JSONDecoder(), [], text.find("{", 1)
    while i >= 0:
        try:
            obj, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            i = text.find("{", i + 1)
            continue
        if isinstance(obj, dict) and "id" in obj and "summary" in obj:
            found.append(obj)
            i = text.find("{", end)
        else:
            i = text.find("{", i + 1)
    return found


def _fix_escapes(text: str) -> str:
    """JSON 에 없는 이스케이프(모델이 마크다운식으로 쓴 '\\-' 등)를 고친다. '\\-' 는 새 글머리를 쓰려던 것이라 줄을 바꾼다.
    올바른 쌍('\\\\', '\\n', '\\"' …)은 앞에서부터 짝지어 읽으므로 건드리지 않는다."""
    def fix(m: re.Match[str]) -> str:
        ch = m.group(1)
        if ch in '"\\/bfnrtu':
            return m.group(0)
        return ("\\n" if ch == "-" else "") + ch
    return re.sub(r"\\(.)", fix, text, flags=re.S)


def parse_entries(raw: Any) -> list[dict[str, str]]:
    """모델 출력이 ```json 울타리·앞뒤 문장을 달고 와도 첫 '{' 부터 마지막 '}' 까지를 읽는다.
    그래도 안 읽히면 닫힌 항목만 건진다 — 잘린 출력 하나 때문에 슬롯의 요약을 모두 버리지 않게."""
    if isinstance(raw, dict):
        data = raw
    else:
        text = _FENCE.sub("", str(raw or "").strip())
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return []
        body = text[start:end + 1]
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            try:
                data = json.loads(_fix_escapes(body))
            except json.JSONDecodeError:
                return _salvage(_fix_escapes(text[start:]))
    entries = data.get("entries") if isinstance(data, dict) else None
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


_ITEM_ID = re.compile(r"f-[0-9A-Za-z]+-\d+")


def match_id(raw_id: str, allowed: list[str]) -> str | None:
    """모델이 id 를 조금 다르게 적어도('[항목 f-…-1]' 처럼 앞뒤에 글자를 붙여도) 이 슬롯 항목 하나로 정해지면 받는다."""
    if raw_id in allowed:
        return raw_id
    found = _ITEM_ID.search(raw_id)
    return found.group(0) if found and found.group(0) in allowed else None


DEBUG_KEEP = 20


def keep_debug(space: str, now: float | None, slot: int, slot_ids: list[str], raw: Any) -> None:
    """요약이 빠진 슬롯의 모델 원문을 남긴다(엔진은 노드 출력을 저장하지 않는다). 최근 DEBUG_KEEP 개만."""
    folder = store.space_dir(space) / "debug"
    stamp = datetime.fromtimestamp(now if now is not None else time.time(), timezone.utc).strftime("%Y%m%d-%H%M%S")
    store.write_atomic(folder / f"merge-{stamp}-slot{slot}.txt",
                       f"ids: {', '.join(slot_ids)}\n--- output ---\n{raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)}")
    for old in sorted(folder.glob("merge-*.txt"))[:-DEBUG_KEEP]:
        old.unlink(missing_ok=True)


def clean_summary(text: str) -> str:
    """요약 안의 제목 줄은 굵은 글씨로 바꾼다 — 절 경계(## [Part N])는 코드만 만든다."""
    lines = []
    for line in str(text or "").strip().splitlines():
        stripped = line.lstrip()
        lines.append(f"**{stripped.lstrip('#').strip()}**" if stripped.startswith("#") else line.rstrip())
    return clip("\n".join(lines).strip(), SUMMARY_CHARS)


_PART_HEAD = re.compile(r"^## \[Part (\d+)\]", re.M)


def split_sections(markdown: str) -> dict[int, str]:
    """기존 문서 노트를 부분 번호별 절로 나눈다."""
    sections: dict[int, str] = {}
    marks = list(_PART_HEAD.finditer(markdown))
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(markdown)
        body = markdown[mark.start():end].strip()
        body = re.sub(r"\n+---\s*$", "", body).strip()
        sections[int(mark.group(1))] = body
    return sections


def render_file_page(name: str, total: int, sections: dict[int, str]) -> str:
    head = f"# {name}\n> 문서: {name} · {total}개 부분으로 정리\n"
    body = "\n\n".join(sections[n] for n in sorted(sections))
    return head + ("\n" + body + "\n" if body else "")


def merge(space: str, outputs: list[Any], now: float | None = None) -> dict[str, Any]:
    """슬롯 i 의 출력은 슬롯 i 에 넣은 항목만 고칠 수 있다. 빠진 항목은 대기열에 남아 다음 실행이 다시 낸다."""
    inflight = store.read_json(store.inflight_path(space), None)
    if not inflight:
        return {"merged": 0, "missing": 0, "pages": []}
    state = store.load_state(space)
    queued = {item["id"]: item for item in store.list_queue(space)}
    done: dict[str, str] = {}
    blank: set[str] = set()  # 출력이 비었거나 하나도 읽히지 않은 슬롯의 항목
    outputs = list(outputs) + [None] * (len(inflight["slots"]) - len(outputs))
    for n, (slot_ids, raw) in enumerate(zip(inflight["slots"], outputs), 1):
        entries = parse_entries(raw)
        if not entries:
            blank.update(slot_ids)
        for entry in entries:
            item_id = match_id(str(entry.get("id") or ""), slot_ids)
            summary = clean_summary(entry.get("summary") or "")
            if item_id and item_id in queued and summary:
                done[item_id] = summary
        if any(i not in done for i in slot_ids):
            keep_debug(space, now, n, slot_ids, raw)

    date = today(now)
    touched: set[str] = set()
    by_file: dict[str, list[dict[str, Any]]] = {}
    for item_id, summary in done.items():
        item = queued[item_id]
        by_file.setdefault(item["file_id"], []).append({**item, "summary": summary})
    for fid, items in by_file.items():
        rel = f"notes/files/file-{fid}.md"
        path = store.space_dir(space) / rel
        name, total = items[0]["name"], items[0]["total"]
        sections = split_sections(path.read_text(encoding="utf-8")) if path.exists() else {}
        for item in items:
            n = item["number"]
            sections[n] = (f"## [Part {n}] {name} ({n}/{total})\n{item['summary']}\n"
                           f"- **출처**: 문서 {name} {n}/{total} 부분 · 갱신 {date}")
        store.write_atomic(path, render_file_page(name, total, sections))
        touched.add(rel)

    for item_id in done:
        store.dequeue(space, item_id)
        store.clear_failures(state, item_id)
    missing = [i for slot in inflight["slots"] for i in slot if i not in done]
    store.save_state(space, state)
    store.inflight_path(space).unlink(missing_ok=True)
    if missing:  # 빠진 항목은 실패로 센다 — 다음 next_batch 가 inflight 없이 다시 묶는다
        for item_id in missing:
            # 모델이 답했는데 이 항목만 빠뜨렸으면 항목 실패, 슬롯 출력이 통째로 비었으면 따로 센다
            key, limit = ("blank", store.MAX_BLANK) if item_id in blank else ("attempts", store.MAX_ATTEMPTS)
            state[key][item_id] = state[key].get(item_id, 0) + 1
            if state[key][item_id] >= limit:
                store.quarantine(space, item_id)
        store.save_state(space, state)
    write_index(space)
    left = store.list_queue(space)
    return {"merged": len(done), "missing": len(missing), "pages": sorted(touched),
            "pending": len(left), "runs_left": runs_needed(left)}


# ── 인덱스 ───────────────────────────────────────────────────────────────────
def title_and_summary(markdown: str, fallback: str) -> tuple[str, str]:
    lines = markdown.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("# "):
            for rest in lines[i + 1:]:
                text = rest.strip()
                if text and not text.startswith(("#", ">")):
                    # 요약 첫 줄의 '- **요지**: ' 머리는 떼어 문서 소개만 남긴다
                    return line[2:].strip(), re.sub(r"^[-*]\s+(\*\*요지\*\*:\s*)?", "", text)[:100]
            return line[2:].strip(), ""
    return fallback, ""


def write_index(space: str) -> str:
    """index.md 는 notes/ 를 훑어 코드가 만든다. 날짜를 넣지 않아 내용이 같으면 파일도 같다(위키 반영 멱등)."""
    root = store.notes_dir(space)
    lines = ["# LLM 위키 색인", ""]
    pages = sorted(root.glob("files/*.md"))
    if pages:
        lines += ["## 문서"]
        for page in pages:
            title, summary = title_and_summary(page.read_text(encoding="utf-8"), page.stem)
            lines.append(f"- [{title}](./{page.relative_to(root).as_posix()})" + (f" — {summary}" if summary else ""))
    content = "\n".join(lines).rstrip() + "\n"
    store.write_atomic(root / "index.md", content)
    return content
