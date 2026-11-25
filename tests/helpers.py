"""테스트 공용 헬퍼(픽스처가 아닌 순수 함수)."""

from __future__ import annotations

from datetime import date, datetime, timezone

from engine.metrics.fundamentals import AnnualFundamentals
from engine.metrics.valuation import ValuationInputs


def freeze_runner_clock(monkeypatch, moment: str) -> None:
    """실행기(`scripts.run_daily`)가 보는 '지금'을 시나리오 시각으로 고정한다.

    여러 테스트가 `2026-11-20` 발행 → `2026-11-23` 주문 마감처럼 **달력 날짜로** 시나리오를 고정한다.
    실제 날짜가 그 마감(동결 시각 = 마감 5분 전)을 지나면 실행기가 만든 레코드의 발행 시각이
    "동결 뒤 도착"이 되어, 대체가 취소가 아니라 `FROZEN_ORDER_STANDS` 로 뒤집히고 다음 세션
    체결도 사라진다 — 시간이 지나면 반드시 깨지는 시한폭탄이다.

    프로덕션 동작(`scripts/run_daily.py`)은 건드리지 않고, 테스트에서만 실행기가 쓰는
    `datetime`/`date` 모듈 속성을 시나리오 시각으로 묶어 어느 날 실행해도 결정론적으로 만든다.
    """
    from scripts import run_daily  # noqa: PLC0415 — 이 헬퍼를 쓰는 테스트에서만 필요하다

    fixed = datetime.fromisoformat(moment.replace("Z", "+00:00")).astimezone(timezone.utc)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.replace(tzinfo=None) if tz is None else fixed.astimezone(tz)

        @classmethod
        def utcnow(cls):
            return fixed.replace(tzinfo=None)

    class _FrozenDate(date):
        @classmethod
        def today(cls):
            return fixed.date()

    monkeypatch.setattr(run_daily, "datetime", _FrozenDatetime, raising=False)
    monkeypatch.setattr(run_daily, "date", _FrozenDate, raising=False)


def annual_fundamentals() -> AnnualFundamentals:
    """Q 점수·하드게이트가 통과하는 우량주 재무(EBIT 5,000·FCF 3,100 수준)."""
    return AnnualFundamentals(
        ebit=[5000, 4900, 4800, 4700, 4600],
        revenue=[25000, 24000, 23000, 22000, 21000],
        net_income=[3100, 3000, 2900, 2800, 2700],
        ocf=[3800, 3700, 3600, 3500, 3400],
        capex=[700, 690, 680, 670, 660],
        depreciation=[600, 590, 580, 570, 560],
        total_assets=[20000, 19500, 19000, 18500, 18000],
        non_interest_liabilities=[4000, 3900, 3800, 3700, 3600],
        excess_cash=[1000, 950, 900, 850, 800],
        total_debt=[3000, 3000, 3100, 3100, 3200],
        cash=[1200, 1150, 1100, 1050, 1000],
        equity=[9000, 8800, 8600, 8400, 8200],
        diluted_shares=[1000, 1000, 1005, 1010, 1015],
        interest_expense=[150, 150, 155, 160, 165],
    )


def example_inputs() -> ValuationInputs:
    """기획서 3.2.3 예시(XYZ) — EV 65,836 / IV_DCF 60.64 / EPV 38.69 / Graham 37.35."""
    return ValuationInputs(
        ebit=5000, tax_rate=0.21, depreciation=600, capex=700, delta_nwc=100,
        net_debt=5000, minority_interest=200, diluted_shares=1000, wacc=0.09,
        terminal_growth=0.025, growth=0.05, eps=3.10, bps=20.00,
    )
