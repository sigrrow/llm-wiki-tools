"""채팅에 나가는 문장은 여기서만 만든다. 도구 이름·경로·내부 id 는 넣지 않고, 남은 일이 있으면 어떻게 이어지는지 붙인다."""
from __future__ import annotations

import time
from typing import Any

from . import store
from .pipeline import split_sections


def synth_report(batch: dict[str, Any], merged: dict[str, Any]) -> str:
    lines = [f"LLM 위키를 정리했어요. 이번에 {merged.get('merged', 0)}건"
             + (f", 노트 {len(merged['pages'])}장 갱신" if merged.get("pages") else "") + "."]
    if not batch.get("taken") and not merged.get("pending"):
        lines = ["정리할 문서가 없어요. llm-wiki-add 로 문서를 더하면 다음 실행에서 정리해요."]
    pending = merged.get("pending", batch.get("left_after", 0))
    if pending:
        runs = merged.get("runs_left") or 1
        lines.append(f"- 정리 대기: {pending}건 — llm-wiki-synth 를 {runs}번 더 실행하면(예약해 뒀으면 다음 예약 때) 이어서 정리해요.")
    if merged.get("missing"):
        lines.append(f"- 요약이 돌아오지 않은 {merged['missing']}건은 다음 실행에서 다시 시도해요(대기에 포함).")
    if batch.get("quarantined"):
        lines.append(f"- 계속 정리에 실패한 {len(batch['quarantined'])}건은 따로 보관했어요. 원문이 너무 길거나 형식이 특이할 수 있어요 — 관리자에게 알려 주세요.")
    return "\n".join(lines)


def doc_labels(docs: list[dict[str, Any]]) -> dict[str, str]:
    """remove 의 files= 에 그대로 넣을 수 있는 이름. 직접 쓴 글은 text/<제목>, 같은 이름의 파일이 여럿이면 <chat_id>/<이름>."""
    names = [d["name"] for d in docs if d.get("channel") != "text"]
    return {d["id"]: f"text/{d['name']}" if d.get("channel") == "text"
            else (f"{d['channel']}/{d['name']}" if names.count(d["name"]) > 1 else d["name"]) for d in docs}


def _doc_line(space: str, doc: dict[str, Any], label: str, state: dict[str, Any],
              waiting: dict[str, int], failed: dict[str, int]) -> str:
    fid = doc["id"]
    total = (state["files"].get(fid) or {}).get("parts")
    page = store.notes_dir(space) / f"files/file-{fid}.md"
    done = len(split_sections(page.read_text(encoding="utf-8"))) if page.exists() else 0
    line = f"  - {label} · " + (f"{total}부분 중 {done} 정리" if total else "상태 없음(다시 add 하세요)")
    if waiting.get(fid):
        line += f" · 대기 {waiting[fid]}"
    if failed.get(fid):
        line += f" · 정리 실패 {failed[fid]}(다시 add 하면 다시 정리해요)"
    return line


def _count_by_doc(folder) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in folder.glob("*.json") if folder.is_dir() else []:
        item = store.read_json(path, None)
        if isinstance(item, dict) and item.get("file_id"):
            counts[item["file_id"]] = counts.get(item["file_id"], 0) + 1
    return counts


def spaces_report() -> str:
    """디스크의 위키(space)마다 요약 한 줄과 문서별 정리 상태. 설정이 없는 폴더는 건너뛴다."""
    spaces = sorted(p.name for p in store.BASE.glob("*") if (p / "config.json").is_file()) if store.BASE.is_dir() else []
    if not spaces:
        return "아직 만든 LLM 위키가 없어요. llm-wiki-init 으로 시작하세요."
    lines = [f"LLM 위키 {len(spaces)}개"]
    for space in spaces:
        config, state = store.load_config(space) or {}, store.load_state(space)
        docs = config.get("files") or []
        notes = [p for p in store.notes_dir(space).rglob("*.md") if p.name != "index.md"]
        waiting = _count_by_doc(store.queue_dir(space))
        failed = _count_by_doc(store.space_dir(space) / "quarantine")
        head = f"- {space} · " + (f"문서 {len(docs)}개 · 노트 {len(notes)}장 · 정리 대기 {sum(waiting.values())}건"
                                  if docs else "문서 없음")
        if notes:
            last = max(p.stat().st_mtime for p in notes)
            head += f" · 마지막 정리 {time.strftime('%Y-%m-%d %H:%M', time.localtime(last))}"
        lines.append(head)
        labels = doc_labels(docs)
        lines += [_doc_line(space, d, labels[d["id"]], state, waiting, failed) for d in docs]
    lines.append("레시피에서 space 를 비우면 main 을 써요. 문서를 뺄 때는 위 이름을 llm-wiki-remove files= 에 그대로 넣으세요.")
    return "\n".join(lines)
