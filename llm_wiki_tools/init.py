"""llm-wiki-add: 만들어 둔 위키에 문서 소스를 더한다(파일 처리에 pypdf·defusedxml 이 필요해 이 서버에 둔다).
llm-wiki-init 은 이 서버를 쓰지 않는다 — 레시피의 bash 노드가 위키 공간(config.json·notes/index.md)을 만든다.

문서(files)는 ProjectXClaw 에 올린 파일을 찾아 글자를 뽑아 바로 대기열에 넣는다(files.py). 이미 등록된 문서를 다시 넣으면 새로 읽는다.
직접 쓴 글(text)은 제목(title, 없으면 첫 줄)을 이름으로 한 문서로 다룬다 — 같은 제목으로 다시 넣으면 그 글을 새 내용으로 바꾼다.
소스는 늘 기존 목록에 더한다. 빼는 것은 llm-wiki-remove 만 한다 — 목록에서 그냥 빼면 그 소스로 만든 노트가 고아로 남는다.
"""
from __future__ import annotations

from typing import Any

from . import files, pipeline, store


def as_ids(values: Any) -> list[str]:
    if isinstance(values, str):
        values = values.split(",")
    return [v.strip() for v in values or [] if str(v).strip()]


TEXT_CHANNEL = "text"  # 직접 쓴 글의 config.files[].channel — remove 에서 `text/<제목>` 으로도 가리킬 수 있다
MAX_TITLE = 60
MAX_TEXT_CHARS = files.MAX_PARTS * files.PART_CHARS  # 100,000자 — 정리할 수 있는 최대치


def text_doc(text: Any, title: Any = "") -> dict[str, Any] | None:
    """직접 쓴 글 → 문서 하나. 글이 비면 None. 제목이 없으면 첫 줄(마크다운 머리표 뗌)을 쓴다."""
    body = str(text or "").strip()
    if not body:
        return None
    name = " ".join(str(title or "").split()) or body.splitlines()[0].lstrip("#").strip()
    name = name[:MAX_TITLE].strip() or "메모"
    # 파일과 달리 앞부분만 정리하지 않고 받지 않는다 — 글은 사용자가 바로 고쳐 다시 넣을 수 있다.
    if len(body) > MAX_TEXT_CHARS:
        raise ValueError(f"text 가 {len(body):,}자로 {MAX_TEXT_CHARS:,}자를 넘는다 — 나눠 넣거나 파일로 올려라")
    if files.split_parts(body)[1]:
        raise ValueError(f"text 가 문단 경계에서 {files.MAX_PARTS}부분({files.PART_CHARS:,}자씩)을 넘게 나뉜다 — "
                         "빈 줄로 나뉜 문단을 줄이거나 나눠 넣어라")
    return {"id": files.file_id(TEXT_CHANNEL, name), "name": name, "channel": TEXT_CHANNEL, "text": body}


def synth_hint(queued: list[dict[str, Any]]) -> str:
    """정리 대기를 비우려면 synth 를 몇 번 돌려야 하는지 알린다. 예약해 뒀으면 기다려도 된다."""
    if not queued:
        return "정리할 부분이 없어요."
    runs = pipeline.runs_needed(queued)
    return (f"정리 대기 {len(queued)}부분 — llm-wiki-synth 를 {runs}번 실행하면 모두 정리돼요"
            " (예약해 뒀으면 예약 실행이 이어서 정리해요).")


def add(space: str, file_refs: Any, text: Any = "", title: Any = "") -> str:
    """올린 파일·직접 쓴 글을 설정에 더하고 대기열에 넣는다. 하나도 못 읽으면 아무것도 저장하지 않는다."""
    config = store.load_config(space)
    if not config:
        raise ValueError(f"'{space}' 위키가 아직 없다 — llm-wiki-init 을 먼저 실행하라")
    refs = files.parse_refs(as_ids(file_refs))
    written = text_doc(text, title)
    if not refs and not written:
        raise ValueError("files·text 가 모두 비었다 — 더할 파일 이름이나 글을 하나 이상 넣어라")
    docs, bad_docs = files.read_all(refs)
    if written:
        docs.append(written)
    if not docs:
        raise ValueError("넣은 문서를 하나도 읽지 못했다 — " + "; ".join(bad_docs[:3]))
    known = {doc["id"] for doc in config.get("files", [])}
    config = {**config, "files": config.get("files", []) + [{k: doc[k] for k in ("id", "name", "channel")}
                                                            for doc in docs if doc["id"] not in known]}
    if len(config["files"]) > files.MAX_FILES:
        raise ValueError(f"기존 문서와 합치면 {files.MAX_FILES}개를 넘는다 — llm-wiki-remove 로 먼저 빼라")
    store.write_json(store.config_path(space), config)
    store.notes_dir(space).mkdir(parents=True, exist_ok=True)
    state = store.load_state(space)
    outcome = {doc["name"]: files.enqueue(space, state, doc) for doc in docs}
    store.save_state(space, state)

    lines = ["문서를 더했어요.", f"- 정리할 문서 {len(config['files'])}개: {', '.join(doc['name'] for doc in config['files'])}"]
    for label, kind in (("내용이 그대로라 다시 정리하지 않는 문서", "unchanged"),
                        ("앞서 정리에 실패한 부분만 다시 정리하는 문서", "retried"),
                        (f"너무 길어 앞 {files.MAX_PARTS * files.PART_CHARS:,}자만 정리하는 문서", "cut")):
        listed = [name for name, result in outcome.items() if result == kind]
        if listed:
            lines.append(f"- {label}: {', '.join(listed)}")
    if bad_docs:
        lines.append(f"- 읽지 못해 빼 둔 문서: {'; '.join(bad_docs)}")
    lines.append(synth_hint(store.list_queue(space)))
    return "\n".join(lines)
