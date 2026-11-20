"""test_execution_order_band — 주문 범위의 가격 기준 통일 (R09).

감사 재현: `dma200_sadj=50`, `split_factor=2`, 신호가(raw) 70, 손절거리 0 → 예전 코드는
`stop_min = max(48.5, 70) = 70`, `limit_max = 160` 을 만들고 raw 시가 80 을 **80.08 에 체결**시켰다.
기획서 3.4.4 는 「`DMA200_sadj × 0.97` 과 `P_open` 비교는 같은 `split_adj_ohlc` 기준」이고,
화면에 보여 줄 때만 같은 날 `split_factor` 로 raw 환산한다.

라이브 최신봉은 `split_factor = 1.0` 이라 수치상 무해하지만, 폴드·재생 패널은 미래 분할까지
반영해 만들어지므로 **모든 폴드의 1차 진입 판정이 분할비율만큼 오염**된다.
"""

from __future__ import annotations

import pytest

from engine.backtest.execution import (EntryBand, OrderNotPlaceable, band_from_dict, build_entry_band,
                                       opening_fill)


def test_order_band_split_consistency():
    """감사 시나리오 그대로: sadj 50 / split 2 / raw 신호가 70 → raw 하한 97 에서 미체결."""
    band = build_entry_band(iv_median_sadj=200.0, required_margin=0.20, dma200_sadj=50.0,
                            signal_price_sadj=35.0, stop_distance=0.0, split_factor=2.0)
    assert band.provisional_stop_sadj == pytest.approx(35.0)
    assert band.dma200_floor_sadj == pytest.approx(48.5)
    assert band.stop_min_sadj == pytest.approx(48.5)
    assert band.as_dict()["display_raw"]["stop_min"] == pytest.approx(97.0)

    # raw 시가 80 → sadj 40 < 48.5 → 미체결 (예전에는 80.08 에 체결됐다)
    result = opening_fill(band, open_price=80.0, split_factor_open=2.0)
    assert result["execution_event"] == "no_action"
    assert result["rejected_reason"] == "outside_order_range"
    assert result["open_price_sadj"] == pytest.approx(40.0)

    # sadj 50 (raw 100) 은 범위 안 → 체결, 체결가는 다시 raw 로 돌려 쓴다
    filled = opening_fill(band, open_price=100.0, split_factor_open=2.0)
    assert filled["execution_event"] == "buy_filled"
    assert filled["fill_price_sadj"] == pytest.approx(50.0 * 1.001)
    assert filled["fill_price"] == pytest.approx(50.0 * 1.001 * 2.0)


def test_limit_max_split_consistency():
    band = build_entry_band(iv_median_sadj=200.0, required_margin=0.20, dma200_sadj=50.0,
                            signal_price_sadj=95.0, stop_distance=0.0, split_factor=2.0)
    assert band.limit_max_sadj == pytest.approx(160.0)
    assert band.as_dict()["display_raw"]["limit_max"] == pytest.approx(320.0)
    over = opening_fill(band, open_price=340.0, split_factor_open=2.0)     # sadj 170 > 160
    assert over["execution_event"] == "no_action"
    inside = opening_fill(band, open_price=300.0, split_factor_open=2.0)   # sadj 150
    assert inside["execution_event"] == "buy_filled"
    assert inside["fill_price_sadj"] == pytest.approx(150.0 * 1.001)


def test_no_raw_sadj_mixing():
    """raw 인자를 받지 않는다 — 이름으로 강제한다(2.4.1 의 PriceKind 규율)."""
    with pytest.raises(TypeError):
        build_entry_band(iv_median=200.0, required_margin=0.20, dma200_sadj=50.0,
                         signal_price_sadj=95.0, stop_distance=0.0)
    with pytest.raises(TypeError):
        build_entry_band(iv_median_sadj=200.0, required_margin=0.20, dma200_sadj=50.0,
                         signal_price=95.0, stop_distance=0.0)
    band = build_entry_band(iv_median_sadj=200.0, required_margin=0.20, dma200_sadj=50.0,
                            signal_price_sadj=95.0, stop_distance=0.0)
    assert band.as_dict()["basis"] == "split_adj_ohlc"
    assert "stop_min" not in band.as_dict()            # 최상위 키는 sadj 만 노출한다


def test_live_latest_bar_unaffected():
    """split_factor=1.0 이면 예전과 같은 값·같은 체결 (라이브 최신봉)."""
    band = build_entry_band(iv_median_sadj=120.0, required_margin=0.20, dma200_sadj=90.0,
                            signal_price_sadj=95.0, stop_distance=0.06, split_factor=1.0)
    old_style = build_entry_band(120.0, 0.20, 90.0, 95.0, 0.06)
    assert band.as_dict() == old_style.as_dict()
    filled = opening_fill(band, open_price=95.0)
    assert filled["execution_event"] == "buy_filled"
    assert filled["fill_price"] == pytest.approx(95.0 * 1.001)
    assert opening_fill(band, open_price=97.0)["execution_event"] == "no_action"
    assert opening_fill(band, open_price=85.0)["execution_event"] == "no_action"


def test_order_not_placeable_is_recorded():
    with pytest.raises(OrderNotPlaceable):
        build_entry_band(iv_median_sadj=100.0, required_margin=0.20, dma200_sadj=90.0,
                         signal_price_sadj=95.0, stop_distance=0.10)


def test_band_dict_roundtrip_and_legacy_migration():
    band = build_entry_band(iv_median_sadj=200.0, required_margin=0.20, dma200_sadj=50.0,
                            signal_price_sadj=95.0, stop_distance=0.0, split_factor=2.0)
    restored = band_from_dict(band.as_dict())
    assert restored == band
    # 구 레코드 형식(stop_min 등)은 sadj 로 읽고 split_factor 1.0 으로 승격한다
    legacy = {"stop_min": 91.0, "limit_max": 112.0, "dma200_floor": 87.3,
              "provisional_stop": 91.0, "required_margin": 0.2}
    migrated = band_from_dict(legacy)
    assert migrated.stop_min_sadj == 91.0 and migrated.split_factor == 1.0
    assert band_from_dict({}) is None and band_from_dict(None) is None


def test_entry_band_accepts_legacy_field_names():
    """모의 계좌·데모가 쓰던 이름을 그대로 받는다(값은 같은 sadj 기준)."""
    band = EntryBand(stop_min=91.0, limit_max=112.0, dma200_floor=87.3,
                     provisional_stop=91.0, required_margin=0.2)
    assert band.stop_min_sadj == 91.0 and band.limit_max == 112.0
    assert band.contains(100.0) is True and band.contains(91.0) is False
