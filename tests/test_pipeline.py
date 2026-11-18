"""레시피 노드 순서대로 도구를 이어 부른다. bash 노드(init·remove)는 YAML 의 명령을 그대로 꺼내 돌리고,
모델 노드 자리는 요약 JSON 을 직접 만든다."""
from __future__ import annotations

import io
import json
import os
import subprocess
import textwrap
import zipfile
from pathlib import Path

import pytest
from defusedxml import DefusedXmlException

from llm_wiki_tools import files, init, pipeline, report, search, store

SPACE = "room-1"
NOW = 1_800_000_000.0
RECIPES = Path(__file__).resolve().parents[3] / "recipes"


def recipe_bash(recipe: str, node_id: str) -> str:
    """레시피 YAML 에서 bash 노드의 command 템플릿(`template: |` 블록)을 꺼낸다(yaml 의존성 없이)."""
    lines = (RECIPES / f"{recipe}.yaml").read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"- id: {node_id}")
    head = next(i for i in range(start, len(lines)) if lines[i].strip() == "template: |")
    indent = len(lines[head]) - len(lines[head].lstrip())
    body = []
    for line in lines[head + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        body.append(line)
    return textwrap.dedent("\n".join(body)) + "\n"


def run_init(space: str = SPACE) -> subprocess.CompletedProcess:
    """llm-wiki-init 의 bash 노드를 엔진처럼 돌린다 — {{args.space}} 를 그대로 끼우고 데이터 폴더에서 bash -c."""
    command = recipe_bash("llm-wiki-init", "n_init").replace("{{args.space}}", space)
    return subprocess.run(["bash", "-c", command], cwd=store.BASE.parent, capture_output=True, text=True, timeout=20)


@pytest.fixture(autouse=True)
def base_dir(tmp_path, monkeypatch):
    """ProjectXClaw 데이터 폴더(tmp/data)와 그 아래 llm-wiki 기본 경로를 흉내 낸다."""
    base = tmp_path / "data" / "llm-wiki"
    base.mkdir(parents=True)
    monkeypatch.setattr(store, "BASE", base)
    return base


@pytest.fixture
def wiki():
    """llm-wiki-init(bash) 으로 만든 빈 위키."""
    done = run_init()
    assert done.returncode == 0, done.stderr


def queue_part(fid: str, n: int, text: str | None = None, total: int | None = None) -> str:
    """add 가 넣는 모양 그대로 문서 부분 하나를 대기열에 넣는다."""
    item_id = f"f-{fid}-{n}"
    store.enqueue(SPACE, {"id": item_id, "kind": "file", "group": fid, "order": n, "number": n, "file_id": fid,
                          "name": f"{fid}.txt", "total": total or n, "text": f"[항목 {item_id}] {text or f'원문 {n}'}"})
    return item_id


def answer_all(batch) -> list[str]:
    """모델 자리: 슬롯에 든 항목마다 요약을 낸다. 첫 슬롯은 ```json 울타리를 단다."""
    outs = []
    for i in range(1, pipeline.SLOTS + 1):
        ids = [line.split("]")[0][4:] for line in batch[f"slot_{i}"].splitlines() if line.startswith("[항목 ")]
        body = json.dumps({"entries": [{"id": x, "summary": f"- **요점**: {x} 요약"} for x in ids]}, ensure_ascii=False)
        outs.append(f"```json\n{body}\n```" if i == 1 else body)
    return outs


def run_synth(now=NOW, outputs=None):
    """llm-wiki-synth.yaml 노드 순서 그대로: next_batch → 요약 8개 → merge → report."""
    batch = pipeline.next_batch(SPACE)
    merged = pipeline.merge(SPACE, outputs(batch) if outputs else answer_all(batch), now=now)
    return {"batch": batch, "merged": merged, "report": report.synth_report(batch, merged)}


# ── init ─────────────────────────────────────────────────────────────────────
def test_init_bash_creates_space_that_tools_read(base_dir):
    done = run_init()
    assert done.returncode == 0, done.stderr
    assert store.load_config(SPACE) == {"files": []}
    index = base_dir / SPACE / "notes" / "index.md"
    assert index.read_text(encoding="utf-8") == pipeline.write_index(SPACE)  # 도구가 다시 만들어도 같은 색인
    assert "만들었어요" in done.stdout and str(index.parent) in done.stdout and "llm-wiki-add" in done.stdout
    assert not (base_dir / SPACE / "config.json.tmp").exists()
    # 방별 AGENTS.md 에 넣을 지침을 보고에 싣는다 — 이름은 넣은 space, 백틱은 실행되지 않고 글자로 남는다
    assert "이 방 AGENTS.md 에 추가해줘" in done.stdout and "## 팀 위키 노트" in done.stdout
    assert f'`mcp_llm-wiki-tools_wiki_search` 를 `space="{SPACE}"`' in done.stdout and "@SPACE@" not in done.stdout
    assert "일반 지식으로 확실히 답할 수 있는 질문이 아니면" in done.stdout and "wiki_next_batch" in done.stdout
    assert "업무" not in done.stdout


def test_init_bash_rerun_changes_nothing(wiki):
    before = store.load_config(SPACE)
    done = run_init()
    assert done.returncode == 0 and store.load_config(SPACE) == before and "이미 있어요" in done.stdout
    assert f'space="{SPACE}"' in done.stdout  # 다시 실행해도 지침을 다시 보여 준다


# 값이 코드에 그대로 끼워지므로 heredoc 종료 줄(`\n__LLM_WIKI_SPACE__\n`)을 넣은 값은 여기서 못 막는다 — 엔진이 인자를
# 셸 인용하거나 환경 변수로 넘겨야 한다. 한 줄짜리 값은 모두 문자열로만 다뤄진다.
@pytest.mark.parametrize("space", ["../..", "", "a b", "x; touch pwned", "$(touch pwned)", "`touch pwned`",
                                   "x'; touch pwned; '", "x\ntouch pwned"])
def test_init_bash_refuses_unsafe_space_without_running_it(base_dir, space):
    done = run_init(space)
    assert done.returncode == 2 and "영문·숫자" in done.stdout
    assert not list(base_dir.iterdir()) and not (base_dir.parent / "pwned").exists()


def test_synth_before_init_stops_with_guidance():
    with pytest.raises(ValueError, match="llm-wiki-init"):
        pipeline.next_batch(SPACE)


def do_add(docs=(), text="", title=""):
    """llm-wiki-add.yaml 의 wiki_add 노드."""
    return init.add(SPACE, list(docs), text, title)


# ── remove ───────────────────────────────────────────────────────────────────
def run_remove(refs, space: str = SPACE) -> subprocess.CompletedProcess:
    """llm-wiki-remove 의 bash 노드를 엔진처럼 돌린다 — space 는 그대로, files(string_list)는 JSON.stringify(v, null, 2) 모양으로 끼운다."""
    command = recipe_bash("llm-wiki-remove", "n_remove").replace("{{args.space}}", space) \
        .replace("{{args.files}}", json.dumps(list(refs), ensure_ascii=False, indent=2))
    return subprocess.run(["bash", "-c", command], cwd=store.BASE.parent, capture_output=True, text=True, timeout=20)


def fake_note(fid: str, title: str) -> None:
    """synth 가 남긴 문서 노트를 흉내 내고 색인을 다시 만든다."""
    page = store.notes_dir(SPACE) / f"files/file-{fid}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(f"# {title}\n\n{title} 요약\n", encoding="utf-8")
    pipeline.write_index(SPACE)


def test_remove_file_deletes_note_queue_state_and_index_line(bot_files):
    assert run_init().returncode == 0
    bot_files("ch-1", "a.txt", "문서 A")
    bot_files("ch-1", "b.txt", "문서 B")
    do_add(docs=("a.txt", "b.txt"))
    fid_a, fid_b = files.file_id("ch-1", "a.txt"), files.file_id("ch-1", "b.txt")
    fake_note(fid_a, "문서 A")
    fake_note(fid_b, "문서 B")
    state = store.load_state(SPACE)
    state["attempts"][f"f-{fid_a}-1"] = 2
    store.save_state(SPACE, state)
    done = run_remove(["a.txt", "없음.txt"])
    assert done.returncode == 0, done.stderr
    assert [d["name"] for d in store.load_config(SPACE)["files"]] == ["b.txt"]
    assert not (store.notes_dir(SPACE) / f"files/file-{fid_a}.md").exists()
    assert not (store.sources_dir(SPACE) / fid_a).exists() and (store.sources_dir(SPACE) / fid_b).exists()
    state = store.load_state(SPACE)
    assert fid_a not in state["files"] and fid_b in state["files"] and state["attempts"] == {}
    assert {i["id"] for i in store.list_queue(SPACE)} == {f"f-{fid_b}-1"}
    index = (store.notes_dir(SPACE) / "index.md").read_text(encoding="utf-8")
    assert "문서 A" not in index and index == pipeline.write_index(SPACE)  # 도구가 다시 만들어도 같은 색인
    assert "문서 a.txt 를 이 위키에서 뺐어요" in done.stdout and "정리 노트 1장, 정리 대기 1건" in done.stdout
    assert "건너뛴 문서: 없음.txt" in done.stdout and "등록된 문서: b.txt" in done.stdout


def test_remove_text_by_title_and_last_one_empties_index():
    assert run_init().returncode == 0
    do_add(text="본문", title="회의 메모")
    fid = files.file_id(init.TEXT_CHANNEL, "회의 메모")
    fake_note(fid, "회의 메모")
    pipeline.next_batch(SPACE)  # 요약 도중 끊긴 실행
    assert store.inflight_path(SPACE).exists()
    done = run_remove(["text/회의 메모"])
    assert done.returncode == 0, done.stderr
    assert "문서 text/회의 메모 를 이 위키에서 뺐어요" in done.stdout and "남은 문서가 없어요" in done.stdout
    assert store.load_config(SPACE)["files"] == [] and store.list_queue(SPACE) == []
    assert not store.inflight_path(SPACE).exists()
    assert (store.notes_dir(SPACE) / "index.md").read_text(encoding="utf-8") == "# LLM 위키 색인\n"


def test_remove_bare_name_drops_every_match_and_quarantined_parts(bot_files):
    assert run_init().returncode == 0
    bot_files("ch-1", "a.txt", "1번 방")
    bot_files("ch-2", "a.txt", "2번 방")
    do_add(docs=("ch-1/a.txt", "ch-2/a.txt"))
    store.quarantine(SPACE, f"f-{files.file_id('ch-1', 'a.txt')}-1")
    done = run_remove(["a.txt"])
    assert done.returncode == 0, done.stderr
    assert store.load_config(SPACE)["files"] == [] and "정리 대기 2건" in done.stdout
    assert not list((store.space_dir(SPACE) / "quarantine").glob("*.json"))


def test_remove_unknown_only_changes_nothing_and_lists_registered(bot_files):
    assert run_init().returncode == 0
    do_add(text="본문", title="메모")
    before = store.load_config(SPACE)
    done = run_remove(["메모2"])
    assert done.returncode == 0 and store.load_config(SPACE) == before
    assert "건너뛴 문서: 메모2" in done.stdout and "등록된 문서: text/메모" in done.stdout


def test_remove_needs_wiki_and_files():
    done = run_remove(["a.txt"])
    assert done.returncode == 2 and "llm-wiki-init" in done.stdout
    assert run_init().returncode == 0
    done = run_remove([])
    assert done.returncode == 2 and "files 에 넣어" in done.stdout


# files 의 줄바꿈은 JSON 이 \n 으로 바꿔 heredoc 을 끝내지 못한다. space 는 init 과 같이 이름 규칙으로 거른다.
@pytest.mark.parametrize("refs", [["x\n__LLM_WIKI_FILES__\ntouch pwned"], ["$(touch pwned)", "`touch pwned`"],
                                  ["\n__LLM_WIKI_PY__\nimport os; os.system('touch pwned')"]])
def test_remove_files_values_never_run_as_code(base_dir, refs):
    assert run_init().returncode == 0
    done = run_remove(refs)
    assert done.returncode == 0 and "건너뛴 문서" in done.stdout
    assert not (base_dir.parent / "pwned").exists()


@pytest.mark.parametrize("space", ["../..", "", "x; touch pwned", "$(touch pwned)"])
def test_remove_refuses_unsafe_space(base_dir, space):
    done = run_remove(["a.txt"], space)
    assert done.returncode == 2 and "영문·숫자" in done.stdout and not (base_dir.parent / "pwned").exists()


# ── synth ────────────────────────────────────────────────────────────────────
def test_full_run_writes_notes_index_and_reports(wiki):
    do_add(text="금요일에는 배포하지 않는다.", title="배포 원칙")
    do_add(text="장애 때는 롤백부터 한다.", title="장애 대응")
    r = run_synth()
    assert r["merged"]["merged"] == 2 and r["merged"]["pending"] == 0 and len(r["merged"]["pages"]) == 2
    index = (store.notes_dir(SPACE) / "index.md").read_text(encoding="utf-8")
    assert "## 문서" in index and "[배포 원칙](./files/file-" in index and index == pipeline.write_index(SPACE)
    assert pipeline.title_and_summary("# 배포 원칙\n> 문서\n\n## [Part 1] x\n- **요지**: 금요일 배포 금지 규칙\n- **예외**: 장애", "x") \
        == ("배포 원칙", "금요일 배포 금지 규칙")
    assert r["report"] == "LLM 위키를 정리했어요. 이번에 2건, 노트 2장 갱신." and not store.inflight_path(SPACE).exists()


def test_empty_queue_says_nothing_to_do(wiki):
    r = run_synth()
    assert r["batch"]["taken"] == 0 and r["merged"]["merged"] == 0
    assert "정리할 문서가 없어요" in r["report"] and not store.inflight_path(SPACE).exists()


def test_add_and_synth_reports_say_how_many_runs_are_left(wiki):
    para = "가" * 4_000  # 부분마다 한 문단 — 부분이 커서 슬롯에 하나씩 들어간다
    total = files.MAX_PARTS
    runs = -(-total // pipeline.SLOTS)
    assert runs >= 2  # 슬롯 수를 바꿔도 '몇 번 더' 안내를 시험하도록
    text = do_add(text="\n\n".join([para] * total), title="큰 글")
    assert f"정리 대기 {total}부분 — llm-wiki-synth 를 {runs}번 실행하면" in text and "예약" in text
    r = run_synth()
    assert r["merged"]["merged"] == pipeline.SLOTS and r["merged"]["runs_left"] == runs - 1
    assert f"정리 대기: {total - pipeline.SLOTS}건 — llm-wiki-synth 를 {runs - 1}번 더 실행하면" in r["report"]
    for _ in range(runs - 1):
        last = run_synth()
    assert last["merged"]["pending"] == 0 and "정리 대기" not in last["report"]
    assert "정리할 부분이 없어요" in do_add(text="\n\n".join([para] * total), title="큰 글")


# ── 묶기·합치기 ───────────────────────────────────────────────────────────────
def test_slot_output_cannot_touch_other_slots_and_missing_items_quarantine(wiki):
    queue_part("a", 1)
    queue_part("a", 2)
    # 두 항목 모두 슬롯 1 에 있다 — 슬롯 2 출력이 f-a-1 을 고치려 해도 무시된다(실패 1회째).
    answered = '{"entries":[{"id":"f-z-1","summary":"- 다른 항목"}]}'  # 답은 했지만 슬롯 항목을 빠뜨림
    first = run_synth(outputs=lambda b: [answered, '{"entries":[{"id":"f-a-1","summary":"- 가짜"}]}'])
    assert first["merged"]["merged"] == 0 and first["merged"]["missing"] == 2
    for _ in range(store.MAX_ATTEMPTS - 1):
        assert pipeline.next_batch(SPACE)["taken"] == 2
        pipeline.merge(SPACE, [answered] * pipeline.SLOTS)
    assert pipeline.next_batch(SPACE)["taken"] == 0
    assert len(list((store.space_dir(SPACE) / "quarantine").glob("*.json"))) == 2


def test_interrupted_run_reissues_same_batch_without_counting_item_failure(wiki):
    queue_part("a", 1)
    first = pipeline.next_batch(SPACE)  # 요약 노드에서 끊겼다고 친다(merge 없음)
    second = pipeline.next_batch(SPACE)
    state = store.load_state(SPACE)
    assert first["slot_1"] == second["slot_1"]
    assert state["attempts"] == {} and state["interrupted"] == {"f-a-1": 1}


def test_interrupted_runs_quarantine_only_at_their_own_limit_and_success_clears_count(wiki):
    for n in (1, 2):
        queue_part("a", n)
    for _ in range(store.MAX_ATTEMPTS):  # 예전에는 여기서 묶음 전체가 격리됐다
        pipeline.next_batch(SPACE)
    assert {i["id"] for i in store.list_queue(SPACE)} == {"f-a-1", "f-a-2"}
    ok = '{"entries":[{"id":"f-a-1","summary":"- 됨"}]}'
    pipeline.merge(SPACE, [ok] + [""] * (pipeline.SLOTS - 1), now=NOW)
    state = store.load_state(SPACE)
    assert "f-a-1" not in state["interrupted"] and state["interrupted"]["f-a-2"] == store.MAX_ATTEMPTS - 1
    for _ in range(store.MAX_INTERRUPTED - state["interrupted"]["f-a-2"] + 1):  # 첫 호출은 묶음만 낸다
        pipeline.next_batch(SPACE)
    assert store.list_queue(SPACE) == [] and (store.space_dir(SPACE) / "quarantine" / "f-a-2.json").exists()


def test_release_quarantine_drops_superseded_copies_and_requeues_the_rest():
    for n in (1, 2, 3):
        queue_part("a", n)
        store.quarantine(SPACE, f"f-a-{n}")
    fresh = {**store.read_json(store.quarantine_path(SPACE, "f-a-1"), None), "text": "[항목 f-a-1] 새 원문"}
    store.write_json(store.queue_dir(SPACE) / "f-a-1.json", fresh)  # 격리 뒤 새 원문이 들어와 있다
    state = store.load_state(SPACE)
    state["attempts"].update({"f-a-1": 3, "f-a-3": 3})  # f-a-2 는 뒤에 요약돼 실패 기록이 지워졌다
    state["interrupted"]["f-a-3"] = 2
    store.save_state(SPACE, state)
    assert store.release_quarantine(SPACE) == {"dropped": ["f-a-1", "f-a-2"], "requeued": ["f-a-3"]}
    state = store.load_state(SPACE)
    assert not list((store.space_dir(SPACE) / "quarantine").glob("*.json"))
    assert {i["id"] for i in store.list_queue(SPACE)} == {"f-a-1", "f-a-3"}
    assert all("f-a-3" not in state[key] for key in store.FAILURE_KEYS)


def test_slots_stay_under_llm_input_cap(wiki):
    for n in range(40):
        queue_part("a", n, "가" * 3000)
    batch = pipeline.next_batch(SPACE)
    assert all(len(batch[f"slot_{i}"]) <= 8000 for i in range(1, pipeline.SLOTS + 1))
    assert batch["taken"] == 2 * pipeline.SLOTS and batch["left_after"] == 40 - 2 * pipeline.SLOTS


def test_big_document_does_not_starve_others_in_summary(wiki):
    for n in range(1, 11):
        queue_part("big", n)
    queue_part("small", 1)
    assert [i["id"] for i in store.list_queue(SPACE)][:3] == ["f-big-1", "f-small-1", "f-big-2"]


# ── ask ──────────────────────────────────────────────────────────────────────
def test_search_matches_spacing_variants_and_reports_no_hit(wiki):
    do_add(text="투라인 클라우드는 TTA 망이다.", title="환경 메모")
    run_synth(outputs=lambda b: [o.replace(" 요약", " 투라인 클라우드는 TTA 망") for o in answer_all(b)])
    found = search.search(SPACE, "투라인클라우드는 어디 환경이야?")
    assert "환경 메모" in found["context"] and "투라인클라우드" in found["matched_terms"]
    none = search.search(SPACE, "카프카 클러스터 버전은?")
    assert none["context"] == "(일치하는 절 없음)" and none["hit_count"] == 0


def test_search_falls_back_to_source_excerpt_for_terms_missing_from_summary(wiki):
    body = ("서론 " * 300 + "\n\n배포 창구는 플랫폼팀 김담당이고 롤백 목표는 5분이다.\n\n" + "결론 " * 300).strip()
    do_add(text=body, title="배포 안내")
    run_synth(outputs=lambda b: [o.replace(" 요약", " 배포 절차 안내") for o in answer_all(b)])
    fid = files.file_id(init.TEXT_CHANNEL, "배포 안내")
    assert (store.sources_dir(SPACE) / fid / "1.txt").read_text(encoding="utf-8") == body
    found = search.search(SPACE, "롤백 목표는 몇 분이야?")
    assert "[원문 · 배포 안내 1/1 부분]" in found["context"] and "롤백 목표는 5분" in found["context"]
    assert "롤백" in found["matched_terms"]
    snippet = found["context"].split("[원문 · 배포 안내 1/1 부분]\n", 1)[1]
    assert snippet.startswith("… ") and snippet.endswith(" …") and len(snippet) < 800  # 맞은 곳 앞뒤만
    both = search.search(SPACE, "배포 절차")
    assert both["context"].startswith("[배포 안내 · notes/files/")  # 요약 절이 먼저


def test_source_parts_follow_readd_and_remove(bot_files, wiki):
    bot_files("ch-1", "설계.md", "\n\n".join(f"{n}번 문단 " + "가" * 4000 for n in range(1, 4)))
    do_add(docs=("설계.md",))
    fid = files.file_id("ch-1", "설계.md")
    folder = store.sources_dir(SPACE) / fid
    assert sorted(p.name for p in folder.glob("*.txt")) == ["1.txt", "2.txt", "3.txt"]
    bot_files("ch-1", "설계.md", "짧은 새 판 키워드")
    do_add(docs=("설계.md",))
    assert sorted(p.name for p in folder.glob("*.txt")) == ["1.txt"]
    assert "키워드" in search.search(SPACE, "키워드")["context"]
    state = store.load_state(SPACE)
    del state["files"][fid]  # 등록이 빠진 문서의 남은 원문은 검색하지 않는다
    store.save_state(SPACE, state)
    assert search.search(SPACE, "키워드")["context"] == "(일치하는 절 없음)"


def test_space_and_ids_cannot_escape_base():
    with pytest.raises(ValueError):
        store.safe_segment("../..")
    assert store.safe_segment("../etc") == "etc"


# ── 문서(대화에 올린 파일) ───────────────────────────────────────────────────
@pytest.fixture
def bot_files(base_dir, monkeypatch):
    """ProjectXClaw 업로드 폴더(data/uploads/<chat_id>/<file_id>_<이름>)를 흉내 낸다. prefix='' 는 웹 업로드 모양."""
    root = base_dir.parent / "uploads"
    monkeypatch.setattr(files, "UPLOADS", root)
    clock = iter(range(1_000, 10_000))

    def put(channel, name, data, prefix="F1_"):
        path = root / channel / f"{prefix}{name}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
        stamp = next(clock)
        os.utime(path, (stamp, stamp))  # 올린 순서를 mtime 으로 분명히 한다
        return path
    return put


def test_add_needs_wiki_and_files_and_respects_limit(bot_files, monkeypatch):
    with pytest.raises(ValueError, match="llm-wiki-init"):
        do_add(docs=("a.txt",))
    run_init()
    with pytest.raises(ValueError, match="files·text 가 모두 비었다"):
        do_add(docs=(), text="  \n ")
    with pytest.raises(ValueError, match="하나도 읽지 못했다.*올린 파일 중에 없음"):
        do_add(docs=("없음.txt",))
    assert store.load_config(SPACE)["files"] == []
    monkeypatch.setattr(files, "MAX_FILES", 1)
    bot_files("ch-1", "a.txt", "A")
    bot_files("ch-1", "b.txt", "B")
    do_add(docs=("a.txt",))
    with pytest.raises(ValueError, match="llm-wiki-remove 로 먼저 빼라"):
        do_add(docs=("b.txt",))
    assert [d["name"] for d in store.load_config(SPACE)["files"]] == ["a.txt"]


def zipped(entries: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, text in entries.items():
            archive.writestr(name, text)
    return buf.getvalue()


def tiny_pdf(text: str) -> bytes:
    """글자 한 줄짜리 PDF(xref 오프셋까지 맞춘 최소 형태)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def test_extract_reads_text_docx_hwpx_pdf_and_refuses_others():
    assert files.extract("메모.txt", "배포는 금요일\r\n\r\n\r\n담당 홍길동".encode("cp949")) == "배포는 금요일\n\n담당 홍길동"
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    docx = zipped({"word/document.xml": f'<w:document {w}><w:body><w:p><w:r><w:t>첫 문단</w:t></w:r></w:p>'
                                        f'<w:tbl><w:tr><w:tc><w:p><w:r><w:t>표 칸</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
                                        f'</w:body></w:document>'})
    assert files.extract("a.docx", docx) == "첫 문단\n표 칸"
    hp = 'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph"'
    hwpx = zipped({"Contents/section10.xml": f'<hp:sec {hp}><hp:p><hp:run><hp:t>둘째 구역</hp:t></hp:run></hp:p></hp:sec>',
                   "Contents/section2.xml": f'<hp:sec {hp}><hp:p><hp:run><hp:t>첫째 구역</hp:t></hp:run></hp:p></hp:sec>'})
    assert files.extract("b.hwpx", hwpx) == "첫째 구역\n\n둘째 구역"
    assert "GPU quota policy" in files.extract("c.pdf", tiny_pdf("GPU quota policy"))
    with pytest.raises(ValueError, match="hwpx"):
        files.extract("d.hwp", b"x")
    with pytest.raises(ValueError, match="지원하지 않는 형식"):
        files.extract("e.exe", b"x")
    bomb = '<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><w:document><w:t>&b;</w:t></w:document>'
    with pytest.raises(DefusedXmlException):  # 엔티티 폭탄은 펼치지 않고 거부한다
        files.extract("f.docx", zipped({"word/document.xml": bomb}))


def test_add_file_queues_parts_and_synth_writes_document_page(bot_files, wiki):
    bot_files("ch-1", "설계.md", "# 설계\n\n" + "\n\n".join(f"{n}번 문단 " + "가" * 1500 for n in range(1, 7)))
    text = do_add(docs=("설계.md",))
    assert store.load_config(SPACE)["files"] == [{"id": files.file_id("ch-1", "설계.md"), "name": "설계.md", "channel": "ch-1"}]
    assert "정리할 문서 1개: 설계.md" in text
    fid = files.file_id("ch-1", "설계.md")
    parts = [i for i in store.list_queue(SPACE) if i["kind"] == "file"]
    assert [i["id"] for i in parts] == [f"f-{fid}-1", f"f-{fid}-2"] and all(i["total"] == 2 for i in parts)
    r = run_synth()
    page = (store.notes_dir(SPACE) / f"files/file-{fid}.md").read_text(encoding="utf-8")
    assert page.startswith("# 설계.md") and "## [Part 1] 설계.md (1/2)" in page and "## [Part 2]" in page
    assert "## 문서" in (store.notes_dir(SPACE) / "index.md").read_text(encoding="utf-8")
    assert r["merged"]["pages"] == [f"notes/files/file-{fid}.md"]
    assert search.search(SPACE, f"f-{fid}-2 요약")["hit_count"] >= 1
    # 같은 내용으로 다시 넣으면 아무것도 다시 하지 않는다
    assert "내용이 그대로라" in do_add(docs=("설계.md",)) and not store.list_queue(SPACE)
    # 짧아진 새 판을 보내 다시 넣으면 그 부분만 다시 정리하고 남는 부분 절은 지운다
    bot_files("ch-1", "설계.md", "짧은 새 판")
    do_add(docs=("설계.md",))
    assert [i["id"] for i in store.list_queue(SPACE)] == [f"f-{fid}-1"]
    run_synth(now=NOW + 7200)
    page = (store.notes_dir(SPACE) / f"files/file-{fid}.md").read_text(encoding="utf-8")
    assert "(1/1)" in page and "[Part 2]" not in page and "1개 부분" in page


def test_file_lookup_strips_upload_prefix_and_picks_latest(bot_files):
    bot_files("slack:C1:1700.1", "회의록.txt", "1차 회의", prefix="F0AAA_")
    bot_files("ch-2", "회의록.txt", "2차 회의", prefix="")  # 웹 업로드 — 접두어 없음
    bot_files("slack:C1:1700.1", "회의록.txt", "3차 회의", prefix="F0BBB_")  # 같은 파일을 다시 보냄
    bot_files("ch-3", "a_회의록.txt", "다른 파일", prefix="F0CCC_")  # 이름 끝만 같은 다른 파일
    assert files.locate("회의록.txt")[:2] == ("slack:C1:1700.1", "회의록.txt")
    assert files.locate("ch-2/회의록.txt")[2].name == "회의록.txt"
    assert files.locate("a_회의록.txt")[0] == "ch-3"
    run_init()
    text = do_add(docs=("회의록.txt", "없는파일.pdf"))
    assert store.load_config(SPACE)["files"] == [{"id": files.file_id("slack:C1:1700.1", "회의록.txt"),
                                                  "name": "회의록.txt", "channel": "slack:C1:1700.1"}]
    assert "읽지 못해 빼 둔 문서: 문서 없는파일.pdf: 올린 파일 중에 없음" in text
    assert "3차 회의" in store.list_queue(SPACE)[-1]["text"]
    for bad in ("../ch-1/회의록.txt", "/etc/passwd", ".env", "a/b/c"):
        with pytest.raises(ValueError, match="파일 이름이나"):
            do_add(docs=(bad,))


def test_add_written_text_as_document_and_replace_by_title(bot_files, wiki):
    bot_files("ch-1", "a.txt", "문서 A")
    text = do_add(docs=("a.txt",), text="# 배포 원칙\n\n금요일에는 배포하지 않는다.")
    names = [(d["channel"], d["name"]) for d in store.load_config(SPACE)["files"]]
    assert names == [("ch-1", "a.txt"), ("text", "배포 원칙")] and "정리할 문서 2개: a.txt, 배포 원칙" in text
    fid = files.file_id("text", "배포 원칙")
    assert "금요일에는" in {i["id"]: i for i in store.list_queue(SPACE)}[f"f-{fid}-1"]["text"]
    run_synth()
    assert (store.notes_dir(SPACE) / f"files/file-{fid}.md").read_text(encoding="utf-8").startswith("# 배포 원칙")
    # 같은 제목으로 다시 넣으면 그 글만 새 내용으로 바뀐다(문서 수는 그대로)
    do_add(text="목요일 오후에도 배포하지 않는다.", title="배포   원칙")
    assert len(store.load_config(SPACE)["files"]) == 2
    assert "목요일" in store.list_queue(SPACE)[-1]["text"]
    queued = len(store.list_queue(SPACE))
    assert "내용이 그대로라 다시 정리하지 않는 문서: 배포 원칙" in do_add(text="목요일 오후에도 배포하지 않는다.", title="배포 원칙")
    assert len(store.list_queue(SPACE)) == queued
    # 제목이 없으면 첫 줄, 너무 길면 자른다
    do_add(text="가" * 100 + "\n둘째 줄")
    assert store.load_config(SPACE)["files"][-1]["name"] == "가" * init.MAX_TITLE


def test_add_rejects_text_over_limit_without_saving(bot_files):
    run_init()
    bot_files("ch-1", "a.txt", "문서 A")
    limit = init.MAX_TEXT_CHARS
    assert limit == 100_000
    with pytest.raises(ValueError, match="100,001자로 100,000자를 넘는다"):
        do_add(docs=("a.txt",), text="가" * (limit + 1))
    para = "나" * 2_600  # 문단 둘이 한 부분에 안 들어가 부분마다 한 문단 — 100,000자 안이어도 20부분을 넘는다
    with pytest.raises(ValueError, match="20부분"):
        do_add(text="\n\n".join([para] * 21))
    assert store.load_config(SPACE)["files"] == [] and not store.list_queue(SPACE)  # 함께 넣은 파일도 저장하지 않는다
    do_add(text="가" * limit)  # 꼭 100,000자는 받는다
    assert len([i for i in store.list_queue(SPACE)]) == files.MAX_PARTS


def test_file_lookup_matches_nfd_name(bot_files):
    import unicodedata
    bot_files("ch-1", "회의록.txt", "본문")
    assert files.locate(unicodedata.normalize("NFD", "회의록.txt"))[1] == "회의록.txt"


def test_zip_bomb_and_symlinked_folder_are_refused(bot_files, monkeypatch, tmp_path):
    monkeypatch.setattr(files, "MAX_XML_BYTES", 1000)
    big = zipped({"word/document.xml": "<w:document>" + "<w:p/>" * 1000 + "</w:document>"})
    with pytest.raises(ValueError, match="너무 큼"):
        files.extract("a.docx", big)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("남의 파일")
    bot_files("ch-1", "ok.txt", "x")
    (files.UPLOADS / "ch-evil").symlink_to(outside)
    (files.UPLOADS / "ch-1" / "F2_link.txt").symlink_to(outside / "secret.txt")
    for ref in ("secret.txt", "ch-evil/secret.txt", "link.txt"):
        with pytest.raises(ValueError, match="올린 파일 중에 없음"):
            files.locate(ref)


def test_quarantined_part_is_retried_by_readd_with_fresh_attempts(bot_files, wiki):
    bot_files("ch-1", "a.txt", "문서 A")
    do_add(docs=("a.txt",))
    fid = files.file_id("ch-1", "a.txt")
    for i in range(3):  # 문서 부분만 계속 요약이 빠진다
        run_synth(now=NOW + i * 7200,
                  outputs=lambda b: [o.replace(f'"f-{fid}-1"', '"x"') for o in answer_all(b)])
    assert not store.list_queue(SPACE) and (store.space_dir(SPACE) / f"quarantine/f-{fid}-1.json").exists()
    assert "실패한 부분만 다시 정리" in do_add(docs=("a.txt",))
    assert [i["id"] for i in store.list_queue(SPACE)] == [f"f-{fid}-1"]
    assert f"f-{fid}-1" not in store.load_state(SPACE)["attempts"]
    run_synth(now=NOW + 99_000, outputs=lambda b: ["", "", "", ""])  # 한 번 빠져도 바로 격리되지 않는다
    assert [i["id"] for i in store.list_queue(SPACE)] == [f"f-{fid}-1"]
    run_synth(now=NOW + 199_000)
    assert (store.notes_dir(SPACE) / f"files/file-{fid}.md").exists()


def test_readd_during_summary_drops_stale_batch(bot_files, wiki):
    bot_files("ch-1", "a.txt", "옛 판")
    do_add(docs=("a.txt",))
    batch = pipeline.next_batch(SPACE)  # synth 가 옛 판을 요약하는 사이에
    bot_files("ch-1", "a.txt", "새 판")
    do_add(docs=("a.txt",))
    assert pipeline.merge(SPACE, answer_all(batch), now=NOW)["merged"] == 0  # 옛 요약은 버린다
    fid = files.file_id("ch-1", "a.txt")
    queued = {i["id"]: i for i in store.list_queue(SPACE)}
    assert "새 판" in queued[f"f-{fid}-1"]["text"] and not store.inflight_path(SPACE).exists()


# ── list ─────────────────────────────────────────────────────────────────────
def test_list_shows_each_document_status_with_removable_names(bot_files, wiki):
    store.space_dir("other").mkdir(parents=True)  # 설정 없는 폴더는 위키가 아니다
    bot_files("ch-1", "a.txt", "1번 방")
    bot_files("ch-2", "a.txt", "2번 방")
    bot_files("ch-1", "설계.md", "\n\n".join(f"{n}번 문단 " + "가" * 4000 for n in range(1, 4)))
    do_add(docs=("ch-1/a.txt", "ch-2/a.txt", "설계.md"), text="메모 본문", title="메모")
    fid = files.file_id("ch-2", "a.txt")
    sid = files.file_id("ch-1", "설계.md")
    for _ in range(store.MAX_ATTEMPTS):  # ch-2/a.txt 만 계속 요약이 빠진다
        run_synth(outputs=lambda b: [o.replace(f'"f-{fid}-1"', '"x"') for o in answer_all(b)])
    queue_part(sid, 3)  # 다시 add 한 것처럼 설계.md 의 한 부분이 대기 중
    text = report.spaces_report()
    assert text.startswith("LLM 위키 1개") and "other" not in text
    assert f"- {SPACE} · 문서 4개 · 노트 3장 · 정리 대기 1건 · 마지막 정리" in text
    assert "  - ch-1/a.txt · 1부분 중 1 정리" in text and "  - text/메모 · 1부분 중 1 정리" in text
    assert "  - ch-2/a.txt · 1부분 중 0 정리 · 정리 실패 1(다시 add 하면" in text
    assert "  - 설계.md · 3부분 중 3 정리 · 대기 1" in text and "llm-wiki-remove files=" in text


def test_list_says_no_wiki_yet():
    assert "아직 만든 LLM 위키가 없어요" in report.spaces_report()


def test_truncated_output_and_loose_ids_are_salvaged_and_misses_are_logged(wiki):
    for n in (1, 2, 3):
        queue_part("a", n, "짧음")
    pipeline.next_batch(SPACE)
    cut = '{"entries":[{"id":"[항목 f-a-1]","summary":"- 하나"},{"id":"f-a-2 부분","summary":"- 둘"},{"id":"f-a-3","summ'
    merged = pipeline.merge(SPACE, [cut], now=NOW)
    assert merged["merged"] == 2 and merged["missing"] == 1  # 잘린 셋째만 빠진다
    logs = list((store.space_dir(SPACE) / "debug").glob("merge-*-slot1.txt"))
    assert len(logs) == 1 and "f-a-3" in logs[0].read_text(encoding="utf-8")
    assert pipeline.match_id("f-a-9", ["f-a-1"]) is None  # 다른 슬롯·모르는 id 는 여전히 막는다


def test_blank_slot_output_is_not_counted_as_item_failure(wiki):
    queue_part("a", 1, "가" * 4000)
    queue_part("b", 1, "가" * 4000)

    def outputs(batch):
        # f-a-1 이 든 슬롯은 답했지만 빠뜨리고, f-b-1 이 든 슬롯은 출력이 비었다(빈 entries 도 같다)
        return ['{"entries":[{"id":"f-z-1","summary":"- x"}]}' if "f-a-1" in batch[f"slot_{i}"]
                else '{"entries":[]}' for i in range(1, pipeline.SLOTS + 1)]
    for _ in range(store.MAX_BLANK):
        pipeline.merge(SPACE, outputs(pipeline.next_batch(SPACE)), now=NOW)
        if _ == store.MAX_ATTEMPTS - 1:  # 항목 실패 3번 — f-a-1 만 격리되고 f-b-1 은 아직 대기
            assert [i["id"] for i in store.list_queue(SPACE)] == ["f-b-1"]
    state = store.load_state(SPACE)
    assert state["attempts"].get("f-b-1") is None and state["blank"]["f-b-1"] == store.MAX_BLANK
    assert {p.name for p in (store.space_dir(SPACE) / "quarantine").glob("*.json")} == {"f-a-1.json", "f-b-1.json"}


def test_invalid_markdown_escape_is_repaired():
    raw = '{"entries":[{"id":"t-1","summary":"- **상태**: 진행\\- **완료**: 코드 C:\\\\temp \\"인용\\" \\u00e9 \\*별"}]}'
    [entry] = pipeline.parse_entries(raw)
    assert entry["summary"] == '- **상태**: 진행\n- **완료**: 코드 C:\\temp "인용" é *별'


def test_previously_failed_items_get_their_own_slot(wiki):
    for n in range(1, 6):
        queue_part("a", n, "짧음")
    state = store.load_state(SPACE)
    state["blank"]["f-a-1"] = 1
    state["attempts"]["f-a-2"] = 1
    store.save_state(SPACE, state)
    pipeline.next_batch(SPACE)
    slots = store.read_json(store.inflight_path(SPACE), None)["slots"]
    assert slots[:3] == [["f-a-1"], ["f-a-2"], ["f-a-3", "f-a-4", "f-a-5"]]
