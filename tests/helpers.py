"""테스트 공용 헬퍼(픽스처가 아닌 순수 함수)."""

from __future__ import annotations

from engine.metrics.fundamentals import AnnualFundamentals
from engine.metrics.valuation import ValuationInputs


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
