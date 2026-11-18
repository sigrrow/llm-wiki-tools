"""레시피 엔진이 넘겨 주는 노드 출력을 한 모양으로 편다.

엔진(recipes/node_exec.ts unwrap_mcp_output)은 MCP 봉투를 벗겨 text 를 넘긴다 — json 출력이 문자열로 한 번 더 싸여 올 수 있다.
"""
from __future__ import annotations

import json
from typing import Any


def parse(value: Any) -> Any:
    """JSON 문자열이면 풀고, 풀린 값이 또 JSON 문자열이면 한 번 더 푼다."""
    for _ in range(2):
        if not isinstance(value, str):
            break
        text = value.strip()
        if not text or text[0] not in "[{\"":
            break
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            break
    return value
