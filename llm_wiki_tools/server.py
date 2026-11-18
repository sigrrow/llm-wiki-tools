"""LLM 위키 레시피 노드가 부르는 결정론 도구 MCP 서버. 외부 서비스에 접속하지 않는다(토큰·네트워크 불필요).

add 는 올린 파일·글을 대기열에 넣고(`wiki_add`), synth 는 대기열을 요약 슬롯으로 묶어(`wiki_next_batch`)
모델 요약을 노트에 합친다(`wiki_merge`). ask 는 `wiki_search`, list 는 `wiki_list` 를 쓴다.
실패는 '[code N] 사유' ToolError 로 낸다. 엔진이 노드를 실패시키고 이 문장을 채팅 보고에 붙인다.
  2: 인자·설정 오류  4: 상태 파일 손상
"""
from __future__ import annotations

import json
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from . import init, pipeline, report, search, shapes, store

ERROR_LIMIT = 240

mcp = FastMCP("llm-wiki-tools")


def fail(code: int, message: str) -> ToolError:
    return ToolError(f"[code {code}] {message}"[:ERROR_LIMIT])


def check_space(space: str) -> str:
    try:
        return store.safe_segment(space)
    except ValueError as error:
        raise fail(2, f"space 가 비었거나 쓸 수 없는 값이다: {error}") from error


def obj(value: Any) -> dict[str, Any]:
    parsed = shapes.parse(value)
    return parsed if isinstance(parsed, dict) else {}


async def run(fn, *args):
    try:
        return await anyio.to_thread.run_sync(lambda: fn(*args))
    except ValueError as error:
        raise fail(2, str(error)) from error
    except json.JSONDecodeError as error:
        raise fail(4, f"상태 파일을 읽지 못했다: {error}") from error


# ── add ──────────────────────────────────────────────────────────────────────
@mcp.tool()
async def wiki_add(space: str, files: Any = None, text: str = "", title: str = "") -> str:
    """ProjectXClaw 에 올린 파일(이름으로 찾음)과 직접 쓴 글(text, 제목 title)을 위키 소스로 더하고 정리 대기열에 넣는다.
    채팅 보고 문장을 낸다."""
    return await run(init.add, check_space(space), files, text, title)


# ── synth ────────────────────────────────────────────────────────────────────
@mcp.tool()
async def wiki_next_batch(space: str) -> dict[str, Any]:
    """대기열 앞쪽을 요약 슬롯 16개(각 7,200자 이하)에 나눠 담는다. 빈 슬롯은 '(비어 있음)' 이다."""
    return await run(pipeline.next_batch, check_space(space))


@mcp.tool()
async def wiki_merge(space: str, out_1: Any = None, out_2: Any = None, out_3: Any = None, out_4: Any = None, out_5: Any = None, out_6: Any = None, out_7: Any = None, out_8: Any = None, out_9: Any = None, out_10: Any = None, out_11: Any = None, out_12: Any = None, out_13: Any = None, out_14: Any = None, out_15: Any = None, out_16: Any = None) -> dict[str, Any]:
    """슬롯별 요약 JSON 을 문서 부분 절 단위로 노트에 합치고 색인을 다시 만든다."""
    outputs = [out_1, out_2, out_3, out_4, out_5, out_6, out_7, out_8,
               out_9, out_10, out_11, out_12, out_13, out_14, out_15, out_16]
    return await run(pipeline.merge, check_space(space), outputs)


@mcp.tool()
async def wiki_report(batch: Any = None, merged: Any = None) -> str:
    """이번 실행 결과를 채팅용 문장으로 만든다(모델을 거치지 않는다)."""
    return report.synth_report(obj(batch), obj(merged))


# ── list ─────────────────────────────────────────────────────────────────────
@mcp.tool()
async def wiki_list() -> str:
    """만들어 둔 LLM 위키(space)마다 문서·노트 수·정리 대기를 채팅용 문장으로 낸다."""
    return await run(report.spaces_report)


# ── ask ──────────────────────────────────────────────────────────────────────
@mcp.tool()
async def wiki_search(space: str, question: str) -> dict[str, Any]:
    """질문 낱말로 notes/ 절을 찾아 근거 묶음(7,200자 이하)을 돌려준다."""
    if not question.strip():
        raise fail(2, "question 이 비었다")
    return await run(search.search, check_space(space), question)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
