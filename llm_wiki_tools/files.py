"""문서 소스. ProjectXClaw 에 올린 파일(UPLOADS/<chat_id>/<file_id>_<이름>, 웹 업로드는 <request_id>/<이름>)을
llm-wiki-add 로 등록한다.

레시피는 실행한 방을 모른다(엔진이 chat_id 를 넘기지 않는다). 그래서 모든 업로드 폴더에서 이름으로 찾고, 같은 이름이
여럿이면(같은 파일을 다시 보냈거나 다른 방에 같은 이름이 있으면) 가장 최근 것을 쓴다. `<chat_id>/<이름>` 으로 방을
정할 수도 있다. 이름만 알면 다른 방 파일도 넣을 수 있다는 뜻이다.
업로드 원본은 지워질 수 있으므로, 등록할 때 글자를 뽑아 바로 대기열에 넣는다.
synth 는 원본 파일 없이 대기열에서 이어 받는다. 긴 문서는 PART_CHARS 단위 부분으로 나눠 부분마다 요약한다.
"""
from __future__ import annotations

import io
import os
import re
import unicodedata
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring

from . import store
from .pipeline import SLOT_CHARS, clip, sha, split_sections

# ProjectXClaw 데이터 폴더의 uploads/ — store.BASE 와 같이 서버 cwd 기준이다. 다른 곳이면 LLM_WIKI_UPLOADS.
UPLOADS = Path(os.environ.get("LLM_WIKI_UPLOADS", "data/uploads")).resolve()
# 메신저 업로드는 `<채널 파일 id>_<이름>` 으로 저장된다(slack·dooray). 웹 업로드는 접두어가 없다.
_UPLOAD_PREFIX = re.compile(r"[A-Za-z0-9-]+_")
MAX_FILES = 20
MAX_BYTES = 30 * 1024 * 1024
PART_CHARS = 5_000
MAX_PARTS = 20
TEXT_LIMIT = (MAX_PARTS + 1) * PART_CHARS  # 이만큼 뽑으면 그만 읽는다 — 넘친다는 것만 알면 된다
MAX_XML_BYTES = 20 * 1024 * 1024  # docx·hwpx 본문 XML 의 풀린 크기 합 — 압축 폭탄으로 메모리를 다 쓰지 않게
MAX_SECTIONS = 200
MAX_PDF_PAGES = 2_000
TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".csv", ".tsv", ".json", ".log", ".yaml", ".yml"}
SUPPORTED = "md·txt·csv·json·yaml·pdf·docx·hwpx"


# ── 이름 풀기·찾기 ─────────────────────────────────────────────────────────────
def _plain(segment: str) -> bool:
    return bool(segment) and segment == Path(segment).name and not segment.startswith(".")


def parse_refs(values: list[str]) -> list[str]:
    """`이름` 또는 `<채널id>/<이름>`. 경로 조각·숨김 파일은 받지 않는다."""
    refs = []
    for ref in values:
        parts = ref.split("/")
        if len(parts) > 2 or not all(_plain(p) for p in parts):
            raise ValueError(f"files 는 파일 이름이나 '<채널id>/<이름>' 이어야 한다: {ref!r}")
        refs.append(ref)
    if len(refs) > MAX_FILES:
        raise ValueError(f"문서는 {MAX_FILES}개까지다")
    return list(dict.fromkeys(refs))


def stored_as(stored: str, name: str) -> bool:
    """저장 이름이 `<이름>` 이거나 `<파일 id>_<이름>` 이면 그 파일이다."""
    if stored == name:
        return True
    head = stored[:-len(name)] if stored.endswith(name) else ""
    return bool(head) and _UPLOAD_PREFIX.fullmatch(head) is not None


def locate(ref: str) -> tuple[str, str, Path]:
    """(chat_id, 이름, 경로). 같은 이름이 여럿이면 가장 최근 것. 없으면 사유를 담아 ValueError."""
    channel, _, name = unicodedata.normalize("NFC", ref).rpartition("/")
    root = UPLOADS
    folders = [UPLOADS / channel] if channel else sorted(UPLOADS.glob("*"))
    found = []
    for folder in folders:
        if folder.is_symlink() or not folder.is_dir():
            continue
        for path in folder.iterdir():
            if stored_as(path.name, name) and path.is_file() and not path.is_symlink() \
                    and path.resolve().is_relative_to(root):
                found.append((path.stat().st_mtime, folder.name, path))
    if not found:
        raise ValueError("올린 파일 중에 없음 — 먼저 대화에 파일을 올려라")
    _, chat_id, path = max(found)
    return chat_id, name, path


def file_id(channel: str, name: str) -> str:
    return sha(f"{channel}/{name}")[:12]


# ── 글자 뽑기 ─────────────────────────────────────────────────────────────────
def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _xml_paragraphs(xml: bytes) -> str:
    """docx(w:p/w:t)·hwpx(hp:p/hp:t) 공통 — 이름공간을 떼고 문단(p) 끝마다 줄을 바꾼다. 표 안 문단도 그대로 읽힌다."""
    out: list[str] = []

    def walk(el: ElementTree.Element) -> None:
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "t":
            out.append("".join(el.itertext()))
            return
        for child in el:
            walk(child)
        if tag == "p":
            out.append("\n")

    walk(fromstring(xml))  # 사용자가 보낸 파일이다 — 엔티티 폭탄·외부 엔티티를 막는 파서로 읽는다
    return "".join(out)


def _section_order(name: str) -> int:
    digits = re.findall(r"\d+", name)
    return int(digits[-1]) if digits else 0


def extract(name: str, data: bytes) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in TEXT_SUFFIXES:
        text = _decode(data)
    elif suffix == ".pdf":
        from pypdf import PdfReader
        pages: list[str] = []
        for page in PdfReader(io.BytesIO(data)).pages[:MAX_PDF_PAGES]:
            pages.append(page.extract_text() or "")
            if sum(map(len, pages)) > TEXT_LIMIT:
                break
        text = "\n\n".join(pages)
        if not text.strip():
            raise ValueError("PDF 에 글자가 없음 — 스캔본일 수 있다")
    elif suffix in (".docx", ".hwpx"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if suffix == ".docx":
                parts = ["word/document.xml"]
            else:
                parts = sorted((n for n in archive.namelist() if re.fullmatch(r"Contents/section\d+\.xml", n)),
                               key=_section_order)
            # 헤더의 풀린 크기로 먼저 거른다 — ZipExtFile 은 헤더 크기 이상을 풀지 않는다
            if len(parts) > MAX_SECTIONS or sum(archive.getinfo(n).file_size for n in parts) > MAX_XML_BYTES:
                raise ValueError(f"본문이 너무 큼(풀면 {MAX_XML_BYTES // 1024 // 1024}MB 넘음)")
            text = "\n".join(_xml_paragraphs(archive.read(part)) for part in parts)
    elif suffix == ".hwp":
        raise ValueError("한글 .hwp 는 못 읽음 — 한글에서 .hwpx 나 PDF 로 저장해 다시 보내라")
    else:
        raise ValueError(f"지원하지 않는 형식({suffix or '확장자 없음'}) — {SUPPORTED} 만 된다")
    text = re.sub(r"[ \t]+\n", "\n", text.replace("\r\n", "\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise ValueError("뽑은 글자가 없음")
    return text


def split_parts(text: str) -> tuple[list[str], bool]:
    """문단 경계에서 PART_CHARS 이하로 나눈다. 한 문단이 넘치면 글자 수로 자른다. (부분 목록, 뒤를 버렸는지)."""
    parts: list[str] = []
    current = ""
    for para in text.split("\n\n"):
        while len(para) > PART_CHARS:
            if current:
                parts.append(current)
                current = ""
            parts.append(para[:PART_CHARS])
            para = para[PART_CHARS:]
        if current and len(current) + 2 + len(para) > PART_CHARS:
            parts.append(current)
            current = ""
        current = f"{current}\n\n{para}" if current else para
    if current:
        parts.append(current)
    return parts[:MAX_PARTS], len(parts) > MAX_PARTS


# ── 등록 ─────────────────────────────────────────────────────────────────────
def read_all(refs: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """찾아서 글자까지 뽑는다(아직 아무것도 쓰지 않는다). (읽은 문서, 못 읽은 사유)."""
    good, bad = [], []
    for ref in refs:
        try:
            channel, name, path = locate(ref)
            if path.stat().st_size > MAX_BYTES:
                raise ValueError(f"{MAX_BYTES // 1024 // 1024}MB 를 넘음")
            text = extract(name, path.read_bytes())
        except (ValueError, OSError, zipfile.BadZipFile, ElementTree.ParseError, DefusedXmlException) as error:
            bad.append(f"문서 {ref}: {error}")
            continue
        except Exception as error:  # pypdf 가 깨진 PDF 에 내는 예외는 종류가 많다
            bad.append(f"문서 {ref}: 읽지 못함({type(error).__name__})")
            continue
        good.append({"id": file_id(channel, name), "name": name, "channel": channel, "text": text})
    return good, bad


def save_sources(space: str, fid: str, parts: list[str]) -> None:
    """원문 부분을 ask 검색용으로 둔다. 내용이 그대로여도 다시 쓴다(옛 위키에 없던 원문을 채우는 길). 줄어든 부분은 지운다."""
    folder = store.sources_dir(space) / store.safe_segment(fid)
    for n, part in enumerate(parts, 1):
        store.write_atomic(folder / f"{n}.txt", part)
    for old in folder.glob("*.txt"):
        if not old.stem.isdigit() or int(old.stem) > len(parts):
            old.unlink(missing_ok=True)


def enqueue(space: str, state: dict[str, Any], doc: dict[str, Any]) -> str:
    """바뀐 문서는 부분을 모두 대기열에 넣고 옛 노트를 지운다(옛 요약과 새 원문이 섞이지 않게).
    내용이 그대로여도 노트에도 대기열에도 없는 부분(격리된 것)은 다시 넣는다 — 다시 넣기가 실패한 문서를 살리는 길이다."""
    fid, name = doc["id"], doc["name"]
    digest = sha(doc["text"])
    prev = state["files"].get(fid) or {}
    parts, cut = split_parts(doc["text"])
    total = len(parts)
    save_sources(space, fid, parts)
    page = store.notes_dir(space) / f"files/file-{fid}.md"
    if prev.get("hash") == digest:
        written = set(split_sections(page.read_text(encoding="utf-8"))) if page.exists() else set()
        waiting = {item["id"] for item in store.list_queue(space)}
        redo = [n for n in range(1, total + 1) if n not in written and f"f-{fid}-{n}" not in waiting]
        if not redo:
            return "unchanged"
    else:
        redo = list(range(1, total + 1))
        page.unlink(missing_ok=True)
        for n in range(total + 1, int(prev.get("parts", 0)) + 1):
            store.dequeue(space, f"f-{fid}-{n}")
            store.clear_failures(state, f"f-{fid}-{n}")
    for n in redo:
        text = f"[항목 f-{fid}-{n}] 문서 · 파일: {name} · {n}/{total} 부분\n원문:\n{parts[n - 1]}"
        store.enqueue(space, {"id": f"f-{fid}-{n}", "kind": "file", "group": fid, "order": n, "number": n,
                              "file_id": fid, "name": name, "total": total, "text": clip(text, SLOT_CHARS - 200)})
        store.clear_failures(state, f"f-{fid}-{n}")
    # 요약 중인 synth 가 이 문서의 옛 원문을 들고 있으면 그 묶음을 버린다 — 옛 요약이 새 원문 자리에 들어가지 않게
    inflight = store.read_json(store.inflight_path(space), None)
    if inflight and any(i.startswith(f"f-{fid}-") for slot in inflight["slots"] for i in slot):
        store.inflight_path(space).unlink(missing_ok=True)
    state["files"][fid] = {"hash": digest, "parts": total, "name": name}
    if prev.get("hash") == digest:
        return "retried"
    return "cut" if cut else ("updated" if prev else "created")
