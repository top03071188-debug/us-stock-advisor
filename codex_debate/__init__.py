"""codex_debate — Codex 스킬 3라운드 투자자 토론 (기획서 v1.3.3 §3.11).

구성
- `skills/`  : 페르소나 6종(1R 투자자 4 + 3R 가드·의장 2)
- `prompts/` : 라운드 프롬프트 4종(1R·2R·3R 가드·3R 의장)
- `schemas/` : 라운드 출력 스키마 4종 + `data/debate_reports.json` 파일 스키마
- `context.py`  : 입력 패킷(`context.json`) 생성·뉴스 본문 발췌·프롬프트 인젝션 탐지
- `validate.py` : 스키마·인용·숫자·가격 검증, 뉴스 3상태 판정, 리포트 조립
- `executors.py`: PC Codex CLI / OpenAI API 어댑터 (같은 스키마 강제)
- `run_debate.py`: 오케스트레이터 (git pull → 1R → 2R → 3R → 즉시 append → 검증 → push)

[고정] 토론은 **의견을 새로 만드는 단계가 아니라** 이미 계산된 신호를 흔들어 보는 단계다.
숫자는 `context.json` 이 유일한 출처이고, 코드가 마지막에 다시 거른다.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "1.0.0"
