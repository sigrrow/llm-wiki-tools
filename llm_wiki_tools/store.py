"""방(space)별 디스크 배치와 상태 파일.

BASE/<space>/
  config.json       llm-wiki-init 이 만들고 add·remove 가 고치는 문서 목록
  state.json        문서별 해시·부분 수, 대기 항목 실패 횟수
  queue/<id>.json   요약을 기다리는 항목(문서 한 부분)
  sources/<문서 id>/<n>.txt  add 가 뽑은 원문 부분 — ask 가 요약에 없는 낱말을 찾는다(remove 가 지운다)
  quarantine/       실패가 상한에 닿은 항목 — 다음 실행을 막지 않게 치워 둔다
  inflight.json     LLM 에 넘긴 묶음. merge 가 지운다 — 남아 있으면 앞 실행이 중간에 끊긴 것
  notes/            정리 결과(사람이 읽는 위키)
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

# ProjectXClaw 의 데이터 폴더(서버 cwd 의 ./data, 컨테이너에서는 /app/data) 아래에 둔다. MCP 서버는 서버 cwd 를 물려받으므로
# `uv run --project <소스>` 로 띄운다(`--directory` 는 cwd 를 소스 폴더로 바꿔 버린다). 다른 곳에 두려면 LLM_WIKI_BASE.
BASE = Path(os.environ.get("LLM_WIKI_BASE", "data/llm-wiki")).resolve()
MAX_ATTEMPTS = 3
# 슬롯 출력이 통째로 비면(모델이 답을 추론 칸에 넣는 등) 항목 탓이 아니라 따로 센다. 그래도 끝없이 돌지 않게 상한을 둔다.
MAX_BLANK = 6
# 실행 전체가 끊기면(시간 초과·LLM 오류·빈 출력) 어느 항목 탓인지 알 수 없다. 항목 실패로 세지 않고, 특정 항목이 실행을
# 계속 깨뜨려 대기열이 영영 막히는 경우만 막도록 넉넉한 상한을 따로 둔다.
MAX_INTERRUPTED = 10
FAILURE_KEYS = ("attempts", "blank", "interrupted")

_SAFE = re.compile(r"[^a-zA-Z0-9_-]")


def safe_segment(value: Any) -> str:
    """외부 id 를 경로 조각으로 쓰기 전에 거른다. 거르고 나서 비면 실패다(경로 탈출 차단)."""
    text = _SAFE.sub("", str(value))
    if not text:
        raise ValueError(f"경로에 쓸 수 없는 id: {value!r}")
    return text


def space_dir(space: str) -> Path:
    return BASE / safe_segment(space)


def write_atomic(path: Path, text: str) -> None:
    """중간에 끊겨도 반쯤 쓴 파일이 남지 않게 임시 파일에 쓰고 바꿔 끼운다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_json(path: Path, value: Any) -> None:
    write_atomic(path, json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True))


def load_state(space: str) -> dict[str, Any]:
    state = read_json(space_dir(space) / "state.json", {})
    for key in ("files", *FAILURE_KEYS):
        state.setdefault(key, {})
    return state


def config_path(space: str) -> Path:
    return space_dir(space) / "config.json"


def load_config(space: str) -> dict[str, Any] | None:
    return read_json(config_path(space), None)


def save_state(space: str, state: dict[str, Any]) -> None:
    write_json(space_dir(space) / "state.json", state)


def queue_dir(space: str) -> Path:
    return space_dir(space) / "queue"


def sources_dir(space: str) -> Path:
    return space_dir(space) / "sources"


def notes_dir(space: str) -> Path:
    return space_dir(space) / "notes"


def inflight_path(space: str) -> Path:
    return space_dir(space) / "inflight.json"


def list_queue(space: str) -> list[dict[str, Any]]:
    """대기 항목을 결정론 순서로 돌려준다. 문서끼리 번갈아 세운다 — 큰 문서 하나가 대기열을 다 차지하지 않게."""
    items = [read_json(p, None) for p in sorted(queue_dir(space).glob("*.json"))]
    items = sorted((item for item in items if item), key=lambda item: (item["group"], item["order"], item["id"]))
    turn: dict[str, int] = {}
    ranked = []
    for item in items:  # 문서(group) 안에서 몇 번째인지
        turn[item["group"]] = turn.get(item["group"], -1) + 1
        ranked.append((turn[item["group"]], item["group"], item["id"], item))
    return [entry[-1] for entry in sorted(ranked, key=lambda entry: entry[:3])]


def enqueue(space: str, item: dict[str, Any]) -> None:
    """같은 id 가 이미 기다리고 있으면 새 내용으로 덮는다 — 요약은 항상 최신 원문으로 한 번만 한다.
    격리돼 있던 항목이면 격리 사본을 지운다(새 원문은 새로 시도한다 — 실패 횟수는 호출자가 clear_failures 로 지운다)."""
    write_json(queue_dir(space) / f"{safe_segment(item['id'])}.json", item)
    quarantine_path(space, item["id"]).unlink(missing_ok=True)


def dequeue(space: str, item_id: str) -> None:
    (queue_dir(space) / f"{safe_segment(item_id)}.json").unlink(missing_ok=True)


def clear_failures(state: dict[str, Any], item_id: str) -> None:
    for key in FAILURE_KEYS:
        state[key].pop(item_id, None)


def quarantine_path(space: str, item_id: str) -> Path:
    return space_dir(space) / "quarantine" / f"{safe_segment(item_id)}.json"


def quarantine(space: str, item_id: str) -> None:
    src = queue_dir(space) / f"{safe_segment(item_id)}.json"
    if src.exists():
        dst = quarantine_path(space, item_id)
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dst)


def release_quarantine(space: str) -> dict[str, list[str]]:
    """격리 항목을 정리한다. 대기열에 같은 id 가 있거나 실패 기록이 없으면(뒤에 다시 요약됨) 낡은 사본이라 지우고,
    나머지는 실패 횟수를 지워 대기열로 되돌린다 — 격리 사본이 그 항목의 가장 최근 원문이다."""
    state = load_state(space)
    out: dict[str, list[str]] = {"dropped": [], "requeued": []}
    queue_dir(space).mkdir(parents=True, exist_ok=True)
    for path in sorted((space_dir(space) / "quarantine").glob("*.json")):
        item_id = path.stem
        if (queue_dir(space) / path.name).exists() or not any(item_id in state[key] for key in FAILURE_KEYS):
            path.unlink()
            out["dropped"].append(item_id)
            continue
        clear_failures(state, item_id)
        os.replace(path, queue_dir(space) / path.name)
        out["requeued"].append(item_id)
    save_state(space, state)
    return out
