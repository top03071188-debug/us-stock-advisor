"""test_13f_live — SEC EDGAR 라이브 13F 수집·워치리스트 티커 매칭·run_screen 배선 (오프라인).

검증하는 것(기획서 2.4.4 · 5.5):
1. `config/13f_filers.yaml` 로더(CIK 정규화·중복/오타는 예외)
2. `submissions` → 최근 2개 분기 `13F-HR` 선택(`13F-NT`·10-K 제외, `13F-HR/A` 폴백)
3. `index.json` → 정보표 XML 탐색(`primary_doc.xml` 제외, 이름 판정 + 실제 `<infoTable>` 확인)
4. 45일·`acceptance_datetime` 시점 필터(LA-7)
5. 분기 델타(신규·확대·축소·청산)와 다중 행 합산(같은 CUSIP 분할 신고)
6. `issuer_to_ticker` 정규화 매칭으로 워치리스트 티커(AAPL·MSFT…)가 실제로 겹침 처리
7. `run_screen.py` 라이브 자동 수집·`--13f-only`·실패 시 지난 레이더 유지

네트워크는 쓰지 않는다 — `SecClient` 를 흉내 낸 가짜 클라이언트만 쓴다.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from engine.data.holdings_13f import (
    FILERS_PATH,
    Filing13F,
    Filer13F,
    Holding13F,
    aggregate_holdings,
    build_issuer_ticker_index,
    collect_13f_filings,
    compute_deltas,
    infotable_candidates,
    load_13f_filers,
    load_company_tickers,
    normalize_issuer_name,
    parse_information_table,
    parse_filing,
    radar_payload,
    recent_13f_filings,
    tickers_for_issuer,
)
from scripts import check_commit_scope, run_screen

ROOT = Path(__file__).resolve().parents[1]
DECISION = datetime(2026, 11, 21, tzinfo=timezone.utc)


# ────────────────────────────── SEC 응답 흉내 ──────────────────────────────
class _Response:
    def __init__(self, payload: dict | None = None, text: str | None = None, status: int = 200):
        self.status_code = status
        self._payload = payload
        self.text = text
        self.content = (text or "").encode("utf-8")

    def json(self) -> dict:
        if self._payload is None:
            raise ValueError("JSON 이 없는 응답")
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Limiter:
    def __init__(self) -> None:
        self.wait_calls = 0

    def wait(self) -> None:
        self.wait_calls += 1


class _Session:
    """URL 라우팅 — `index.json` 은 접수번호, `.xml` 은 파일 이름 마지막 조각으로 찾는다."""

    def __init__(self, owner: "FakeSecClient") -> None:
        self.owner = owner

    def get(self, url: str, timeout: int | None = None):  # noqa: ARG002 — SecClient 계약과 같은 서명
        self.owner.requested.append(url)
        if url.endswith("index.json"):
            accn = url.rstrip("/").rsplit("/", 2)[-2]
            payload = self.owner.index.get(accn)
            if payload is None:
                return _Response(status=404)
            return _Response(payload=payload)
        name = url.rsplit("/", 1)[-1]
        for key, xml in self.owner.xml.items():
            if key == url or key == name:
                return _Response(text=xml)
        return _Response(status=404)


class FakeSecClient:
    """`SecClient` 대역 — `submissions()` · `company_tickers()` · `session.get` 만 갖는다."""

    def __init__(self, submissions: dict | None = None, index: dict | None = None,
                 xml: dict | None = None, tickers: list[dict] | None = None,
                 fail_ciks: set[int] | None = None) -> None:
        self._submissions = submissions or {}
        self.index = index or {}
        self.xml = xml or {}
        self._tickers = tickers or []
        self.fail_ciks = fail_ciks or set()
        self.limiter = _Limiter()
        self.session = _Session(self)
        self.requested: list[str] = []
        self.ticker_calls = 0

    def submissions(self, cik: int) -> dict:
        if int(cik) in self.fail_ciks:
            raise RuntimeError(f"HTTP 503 (CIK {int(cik)})")
        payload = self._submissions.get(int(cik))
        if payload is None:
            raise RuntimeError(f"HTTP 404 (CIK {int(cik)})")
        return payload

    def company_tickers(self) -> pd.DataFrame:
        self.ticker_calls += 1
        return pd.DataFrame(self._tickers)


def _submissions(name: str, rows: list[dict]) -> dict:
    """`{name, filings:{recent:{열…}}}` — SEC submissions 응답 형태."""
    recent = {key: [] for key in ("accessionNumber", "form", "reportDate", "filingDate",
                                  "acceptanceDateTime", "primaryDocument")}
    for row in rows:
        recent["accessionNumber"].append(row["accn"])
        recent["form"].append(row["form"])
        recent["reportDate"].append(row.get("report_date", ""))
        recent["filingDate"].append(row.get("filing_date", ""))
        recent["acceptanceDateTime"].append(row.get("acceptance", ""))
        recent["primaryDocument"].append(row.get("primary_doc", "xslForm13F_X02/primary_doc.xml"))
    return {"name": name, "filings": {"recent": recent, "files": []}}


def _infotable(rows: list[tuple[str, str, float, str]]) -> str:
    """`<infoTable>` XML — `(issuer, cusip, value, put_call)` 목록. value 는 **달러** 그대로."""
    body = []
    for issuer, cusip, value, put_call in rows:
        body.append(
            "  <infoTable>"
            f"<nameOfIssuer>{issuer}</nameOfIssuer><titleOfClass>COM</titleOfClass>"
            f"<cusip>{cusip}</cusip><value>{value:.0f}</value>"
            "<shrsOrPrnAmt><sshPrnamt>1000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>"
            f"<putCall>{put_call}</putCall>"
            "</infoTable>"
        )
    return ('<?xml version="1.0"?>\n'
            '<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">\n'
            + "\n".join(body) + "\n</informationTable>\n")


def _index(*names: str) -> dict:
    return {"directory": {"item": [{"name": name, "size": "1000"} for name in names]}}


def _accn_nodash(accn: str) -> str:
    return accn.replace("-", "")


# ────────────────────────────── 펀드 목록 ──────────────────────────────
def test_config_lists_verified_funds():
    filers = load_13f_filers(ROOT / FILERS_PATH)
    ciks = {f.cik for f in filers}
    assert {"0001067983", "0001166559"} <= ciks          # 버크셔·게이츠 재단
    assert len(filers) >= 7
    for filer in filers:
        assert len(filer.cik) == 10 and filer.cik.isdigit()
        assert filer.name and filer.style


def test_loader_rejects_broken_config(tmp_path):
    broken = tmp_path / "filers.yaml"
    broken.write_text("filers:\n  - cik: 'ABC'\n    name: Broken\n", encoding="utf-8")
    with pytest.raises(Exception):
        load_13f_filers(broken)

    duplicate = tmp_path / "dup.yaml"
    duplicate.write_text("filers:\n"
                         "  - {cik: '1067983', name: A}\n"
                         "  - {cik: '0001067983', name: B}\n", encoding="utf-8")
    with pytest.raises(Exception):
        load_13f_filers(duplicate)

    with pytest.raises(Exception):
        load_13f_filers(tmp_path / "missing.yaml")


def test_loader_zero_pads_cik(tmp_path):
    path = tmp_path / "filers.yaml"
    path.write_text("filers:\n  - {cik: '1067983', name: Berkshire, style: value}\n", encoding="utf-8")
    filers = load_13f_filers(path)
    assert filers[0].cik == "0001067983" and filers[0].style == "value"


# ────────────────────────────── submissions → 분기 선택 ──────────────────────────────
def test_recent_filings_skip_notice_and_prefer_original():
    payload = _submissions("BERKSHIRE HATHAWAY INC", [
        {"form": "13F-NT", "report_date": "2026-09-30", "filing_date": "2026-11-14",
         "acceptance": "2026-11-14T21:00:00.000Z", "accn": "0000000000-26-000009"},
        {"form": "13F-HR", "report_date": "2026-09-30", "filing_date": "2026-11-16",
         "acceptance": "2026-11-16T21:00:04.000Z", "accn": "0000000000-26-000010"},
        {"form": "13F-HR/A", "report_date": "2026-09-30", "filing_date": "2026-11-20",
         "acceptance": "2026-11-20T21:00:00.000Z", "accn": "0000000000-26-000011"},
        {"form": "13F-HR", "report_date": "2026-06-30", "filing_date": "2026-08-14",
         "acceptance": "2026-08-14T20:05:04.000Z", "accn": "0000000000-26-000008"},
        {"form": "10-K", "report_date": "", "filing_date": "2026-02-01",
         "acceptance": "2026-02-01T12:00:00.000Z", "accn": "0000000000-26-000001"},
    ])
    rows = recent_13f_filings(payload, quarters=2)
    assert [r["period_end"].isoformat() for r in rows] == ["2026-09-30", "2026-06-30"]
    assert rows[0]["form"] == "13F-HR"                       # 개정판보다 원본
    assert rows[0]["accession"] == "0000000000-26-000010"
    assert rows[1]["filed_date"].isoformat() == "2026-08-14"


def test_recent_filings_fall_back_to_amendment_and_limit_quarters():
    payload = _submissions("COATUE MANAGEMENT LLC", [
        {"form": "13F-HR/A", "report_date": "2026-03-31", "filing_date": "2026-08-01",
         "acceptance": "2026-08-01T12:00:00.000Z", "accn": "0000000000-26-000004"},
        {"form": "13F-HR", "report_date": "2025-12-31", "filing_date": "2026-02-13",
         "acceptance": "2026-02-13T12:00:00.000Z", "accn": "0000000000-26-000003"},
        {"form": "13F-HR", "report_date": "2025-09-30", "filing_date": "2025-11-13",
         "acceptance": "2025-11-13T12:00:00.000Z", "accn": "0000000000-25-000002"},
    ])
    rows = recent_13f_filings(payload, quarters=2)
    assert [r["period_end"].isoformat() for r in rows] == ["2026-03-31", "2025-12-31"]
    assert rows[0]["form"] == "13F-HR/A"                     # 원본이 없을 때만 개정판


# ────────────────────────────── 정보표 XML 탐색 ──────────────────────────────
def test_infotable_candidates_skip_cover_page():
    index = _index("0001193125-26-463399-index.html", "xslForm13F_X02/primary_doc.xml",
                   "primary_doc.xml", "60471.xml", "R1.htm")
    urls = infotable_candidates(index, "0001067983", "0001193125-26-463399")
    assert urls == ["https://www.sec.gov/Archives/edgar/data/1067983/000119312526463399/60471.xml"]


def test_infotable_candidates_prefer_named_infotable():
    index = _index("primary_doc.xml", "60471.xml", "infotable.xml")
    urls = infotable_candidates(index, "0001166559", "0001104659-26-128337")
    assert [u.rsplit("/", 1)[-1] for u in urls] == ["infotable.xml", "60471.xml"]


def test_infotable_candidates_empty_for_notice_filing():
    index = _index("primary_doc.xml", "0001172661-26-005462-index.html")
    assert infotable_candidates(index, "0001336528", "0001172661-26-005462") == []


# ────────────────────────────── 값 단위·다중 행 합산 ──────────────────────────────
def test_value_scale_dollars_vs_thousands():
    xml = _infotable([("APPLE INC", "037833100", 472_572_546, "")])
    assert parse_information_table(xml)[0].value_usd == pytest.approx(472_572_546_000.0)      # 기본 = 천 달러
    assert parse_information_table(xml, value_scale=1.0)[0].value_usd == pytest.approx(472_572_546.0)


def test_parse_handles_commas_and_dollar_prefix():
    xml = ("<informationTable><infoTable><nameOfIssuer>X</nameOfIssuer>"
           "<cusip>AAA</cusip><value>$1,250,000</value></infoTable></informationTable>")
    assert parse_information_table(xml)[0].value_usd == pytest.approx(1_250_000.0)


def test_aggregate_holdings_sums_split_rows():
    rows = [Holding13F(cusip="02005N100", issuer="ALLY FINL INC", value_usd=v)
            for v in (472_572_546.0, 105_481_778.0, 159_064_884.0)]
    rows.append(Holding13F(cusip="02005N100", issuer="ALLY FINL INC", value_usd=10.0, put_call="Put"))
    merged = {(h.cusip, h.put_call): h for h in aggregate_holdings(rows)}
    assert merged[("02005N100", "")].value_usd == pytest.approx(737_119_208.0)
    assert merged[("02005N100", "Put")].value_usd == pytest.approx(10.0)


def test_deltas_sum_split_rows_instead_of_last_wins():
    prev = Filing13F(filer_cik="1", filer_name="F", period_end=date(2026, 6, 30),
                     acceptance_datetime=datetime(2026, 8, 14, tzinfo=timezone.utc),
                     holdings=[Holding13F(cusip="AAA", issuer="A", value_usd=100.0),
                               Holding13F(cusip="AAA", issuer="A", value_usd=100.0)])
    cur = Filing13F(filer_cik="1", filer_name="F", period_end=date(2026, 9, 30),
                    acceptance_datetime=datetime(2026, 11, 16, tzinfo=timezone.utc),
                    holdings=[Holding13F(cusip="AAA", issuer="A", value_usd=150.0),
                              Holding13F(cusip="AAA", issuer="A", value_usd=50.0)])
    row = compute_deltas(prev, cur)[0]
    assert row["prev_value_usd"] == pytest.approx(200.0)
    assert row["value_usd"] == pytest.approx(200.0)
    assert row["kind"] == "unchanged"                     # 200 → 200 (마지막 행만 보면 -25%)


# ────────────────────────────── 라이브 수집기 ──────────────────────────────
def _berkshire_filer() -> Filer13F:
    return Filer13F(cik="0001067983", name="BERKSHIRE HATHAWAY INC", style="value")


def _berkshire_client(quarters: tuple[int, int] = (3, 2)) -> FakeSecClient:
    """버크셔 2개 분기 — Q3/Q2 인디케이터에 따라 값이 바뀐다."""
    q3, q2 = quarters
    accn_q3, accn_q2 = "0001193125-26-463399", "0001193125-26-352200"
    submissions = _submissions("BERKSHIRE HATHAWAY INC", [
        {"form": "13F-HR", "report_date": "2026-09-30", "filing_date": "2026-11-16",
         "acceptance": "2026-11-16T21:00:04.000Z", "accn": accn_q3},
        {"form": "13F-HR", "report_date": "2026-06-30", "filing_date": "2026-08-14",
         "acceptance": "2026-08-14T20:05:04.000Z", "accn": accn_q2},
        {"form": "13F-HR", "report_date": "2026-03-31", "filing_date": "2026-05-15",
         "acceptance": "2026-05-15T20:06:05.000Z", "accn": "0001193125-26-226661"},
    ])
    index = {_accn_nodash(accn_q3): _index("primary_doc.xml", "60471.xml"),
             _accn_nodash(accn_q2): _index("primary_doc.xml", "60471.xml")}
    xml = {
        f"https://www.sec.gov/Archives/edgar/data/1067983/{_accn_nodash(accn_q3)}/60471.xml":
            _infotable([("ALLY FINL INC", "02005N100", 472_572_546 * q3 / 3, ""),
                        ("ALLY FINL INC", "02005N100", 105_481_778 * q3 / 3, ""),
                        ("APPLE INC", "037833100", 20_000_000_000 * q3 / 3, ""),
                        ("CHEVRON CORP", "166764100", 500_000_000 * q3 / 3, "")]),
        f"https://www.sec.gov/Archives/edgar/data/1067983/{_accn_nodash(accn_q2)}/60471.xml":
            _infotable([("ALLY FINL INC", "02005N100", 400_000_000 * q2 / 2, ""),
                        ("ALLY FINL INC", "02005N100", 100_000_000 * q2 / 2, ""),
                        ("APPLE INC", "037833100", 25_000_000_000 * q2 / 2, "")]),
    }
    return FakeSecClient(submissions={1067983: submissions}, index=index, xml=xml)


def test_collect_parses_two_quarters_in_dollars():
    client = _berkshire_client()
    result = collect_13f_filings(DECISION, filers=[_berkshire_filer()], client=client)
    assert result["status"] == "ok" and result["n_filers"] == 1
    assert result["n_filings"] == 2
    latest = max(result["filings"], key=lambda f: f.period_end)
    assert latest.period_end.isoformat() == "2026-09-30"
    assert latest.filer_name == "BERKSHIRE HATHAWAY INC"
    assert latest.form == "13F-HR"
    ally = [h for h in latest.holdings if h.cusip == "02005N100"]
    assert len(ally) == 1                                       # 다중 행은 하나로 합산
    assert ally[0].value_usd == pytest.approx(578_054_324.0)    # 472,572,546 + 105,481,778
    assert ally[0].value_usd < 1e10                             # 천 달러로 곱했다면 5.8e11
    assert result["errors"] == []


def test_collect_applies_acceptance_time_filter():
    client = _berkshire_client()
    before_filing = datetime(2026, 11, 10, tzinfo=timezone.utc)   # Q3 접수(11-16) 이전
    result = collect_13f_filings(before_filing, filers=[_berkshire_filer()], client=client)
    assert [f.period_end.isoformat() for f in result["filings"]] == ["2026-06-30"]
    assert [s["period_end"] for s in result["skipped"]] == ["2026-09-30"]
    assert "미래 분기" in result["skipped"][0]["reason"]


def test_collect_drops_filing_inside_45_day_window():
    """2026-09-30 분기는 +45일(11-14) 전에는 못 쓴다 — 접수(11-16) 전이면 더더욱."""
    client = _berkshire_client()
    result = collect_13f_filings(datetime(2026, 11, 14, tzinfo=timezone.utc),
                                filers=[_berkshire_filer()], client=client)
    assert result["n_filings"] == 1                            # Q2 만
    assert result["filings"][0].period_end.isoformat() == "2026-06-30"


def test_collect_skips_non_infotable_xml_candidate():
    accn = "0001193125-26-463399"
    submissions = _submissions("BERKSHIRE HATHAWAY INC", [
        {"form": "13F-HR", "report_date": "2026-09-30", "filing_date": "2026-11-16",
         "acceptance": "2026-11-16T21:00:04.000Z", "accn": accn},
    ])
    base = f"https://www.sec.gov/Archives/edgar/data/1067983/{_accn_nodash(accn)}"
    client = FakeSecClient(
        submissions={1067983: submissions},
        index={_accn_nodash(accn): _index("primary_doc.xml", "cover.xml", "real.xml")},
        xml={f"{base}/cover.xml": "<informationTable><header><formType>13F-HR</formType></header></informationTable>",
             f"{base}/real.xml": _infotable([("APPLE INC", "037833100", 5_000_000_000, "")])},
    )
    result = collect_13f_filings(DECISION, filers=[_berkshire_filer()], client=client)
    assert result["n_filings"] == 1
    assert result["filings"][0].holdings[0].cusip == "037833100"
    assert any(url.endswith("/real.xml") for url in client.requested)


def test_collect_reports_filing_without_infotable():
    """정보표가 없는 폴더(`primary_doc.xml` 뿐) — 예외 대신 진단으로 남기고 다음 펀드로 간다."""
    accn = "0001172661-26-003777"
    submissions = _submissions("PERSHING SQUARE CAPITAL MANAGEMENT, L.P.", [
        {"form": "13F-HR", "report_date": "2026-06-30", "filing_date": "2026-08-14",
         "acceptance": "2026-08-14T20:23:33.000Z", "accn": accn},
    ])
    client = FakeSecClient(submissions={1336528: submissions},
                           index={_accn_nodash(accn): _index("primary_doc.xml")})
    result = collect_13f_filings(
        DECISION, filers=[Filer13F(cik="0001336528", name="Pershing Square Capital Management")],
        client=client)
    assert result["status"] == "failed"                        # 빈 정상(empty)과 구분한다
    assert result["errors"][0]["stage"] == "infotable"
    assert "정보표" in result["errors"][0]["error"]


def test_collect_falls_back_to_older_hr_when_latest_quarters_are_notices():
    """13F-NT(통지)만 있는 최근 분기는 건너뛰고, 정보표가 있는 가장 최근 13F-HR 을 쓴다."""
    submissions = _submissions("PERSHING SQUARE CAPITAL MANAGEMENT, L.P.", [
        {"form": "13F-NT", "report_date": "2026-09-30", "filing_date": "2026-11-16",
         "acceptance": "2026-11-16T22:30:06.000Z", "accn": "0001172661-26-005462"},
        {"form": "13F-NT", "report_date": "2026-06-30", "filing_date": "2026-08-14",
         "acceptance": "2026-08-14T20:23:33.000Z", "accn": "0001172661-26-003778"},
        {"form": "13F-HR", "report_date": "2026-03-31", "filing_date": "2026-05-15",
         "acceptance": "2026-05-15T20:19:58.000Z", "accn": "0001172661-26-002336"},
    ])
    accn = "0001172661-26-002336"
    base = f"https://www.sec.gov/Archives/edgar/data/1336528/{_accn_nodash(accn)}"
    client = FakeSecClient(submissions={1336528: submissions},
                           index={_accn_nodash(accn): _index("primary_doc.xml", "infotable.xml")},
                           xml={f"{base}/infotable.xml": _infotable([("APPLE INC", "037833100", 5_000_000_000, "")])})
    result = collect_13f_filings(
        DECISION, filers=[Filer13F(cik="0001336528", name="Pershing Square Capital Management")],
        client=client)
    assert result["status"] == "ok" and result["n_filings"] == 1
    assert result["filings"][0].period_end.isoformat() == "2026-03-31"


def test_collect_honours_per_filer_value_unit():
    """같은 `<value>` 라도 펀드별 단위가 다르다 — `value_unit: thousands` 면 1,000배로 읽는다."""
    accn = "0001061768-26-000015"
    submissions = _submissions("BAUPOST GROUP LLC/MA", [
        {"form": "13F-HR", "report_date": "2026-09-30", "filing_date": "2026-11-13",
         "acceptance": "2026-11-13T21:03:21.000Z", "accn": accn},
    ])
    base = f"https://www.sec.gov/Archives/edgar/data/1061768/{_accn_nodash(accn)}"
    client = FakeSecClient(submissions={1061768: submissions},
                           index={_accn_nodash(accn): _index("primary_doc.xml", "baupost13fq32026.xml")},
                           xml={f"{base}/baupost13fq32026.xml":
                                _infotable([("GENUINE PARTS CO", "372460105", 389_540, "")])})
    filer = Filer13F(cik="0001061768", name="Baupost Group", value_unit="thousands")
    result = collect_13f_filings(DECISION, filers=[filer], client=client)
    assert result["status"] == "ok"
    assert result["filings"][0].holdings[0].value_usd == pytest.approx(389_540_000.0)


def test_collect_flags_implausible_value_total():
    """단위가 틀리면 평가액이 비현실적이 된다 — 조용히 넘기지 않고 설정 확인 진단을 남긴다."""
    accn = "0001061768-26-000015"
    submissions = _submissions("BAUPOST GROUP LLC/MA", [
        {"form": "13F-HR", "report_date": "2026-09-30", "filing_date": "2026-11-13",
         "acceptance": "2026-11-13T21:03:21.000Z", "accn": accn},
    ])
    base = f"https://www.sec.gov/Archives/edgar/data/1061768/{_accn_nodash(accn)}"
    client = FakeSecClient(submissions={1061768: submissions},
                           index={_accn_nodash(accn): _index("primary_doc.xml", "infotable.xml")},
                           xml={f"{base}/infotable.xml":
                                _infotable([("GENUINE PARTS CO", "372460105", 389_540, "")])})
    filer = Filer13F(cik="0001061768", name="Baupost Group")          # 단위를 지정하지 않으면 달러로 읽힌다
    result = collect_13f_filings(DECISION, filers=[filer], client=client)
    assert result["status"] == "partial"
    assert [e["stage"] for e in result["errors"]] == ["value_unit"]
    assert "value_unit" in result["errors"][0]["error"]


def test_collect_tolerates_one_filer_failure():
    client = _berkshire_client()
    client._submissions[1166559] = _submissions("GATES FOUNDATION TRUST", [])
    client.fail_ciks = {1166559}
    filers = [f for f in load_13f_filers(ROOT / FILERS_PATH) if f.cik in ("0001067983", "0001166559")]
    result = collect_13f_filings(DECISION, filers=filers, client=client)
    assert result["status"] == "partial"
    assert [e["stage"] for e in result["errors"]] == ["submissions"]
    assert result["n_filings"] == 2


def test_collect_without_user_agent_is_diagnostic_not_exception(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    result = collect_13f_filings(DECISION)
    assert result["status"] == "missing_key"
    assert result["filings"] == []
    assert "SEC_USER_AGENT" in result["errors"][0]["error"]


def test_collect_rejects_naive_decision_at():
    with pytest.raises(Exception):
        collect_13f_filings(datetime(2026, 11, 21))


# ────────────────────────────── 워치리스트 매칭 ──────────────────────────────
def test_normalize_issuer_name_variants():
    assert normalize_issuer_name("Apple Inc.") == normalize_issuer_name("APPLE INC CLASS A") == "APPLE"
    assert normalize_issuer_name("BERKSHIRE HATHAWAY INC DEL") == "BERKSHIRE HATHAWAY"
    assert normalize_issuer_name("JPMorgan Chase & Co.") == normalize_issuer_name("JPMORGAN CHASE & CO")
    assert normalize_issuer_name("THE HOME DEPOT INC") == "HOME DEPOT"


def test_issuer_index_handles_multiple_share_classes():
    index = build_issuer_ticker_index([
        {"ticker": "GOOGL", "title": "Alphabet Inc."},
        {"ticker": "GOOG", "title": "Alphabet Inc."},
        {"ticker": "AAPL", "title": "Apple Inc."},
        {"ticker": "MSFT", "title": "MICROSOFT CORP"},
    ])
    assert index["ALPHABET"] == ["GOOG", "GOOGL"]
    assert tickers_for_issuer("ALPHABET INC", index) == ["GOOG", "GOOGL"]
    assert tickers_for_issuer("MICROSOFT CORP", index) == ["MSFT"]
    assert tickers_for_issuer("SOME UNKNOWN CO", index) == []


def test_radar_matches_watchlist_tickers_by_issuer_name():
    prev = Filing13F(filer_cik="0001067983", filer_name="Berkshire Hathaway",
                     period_end=date(2026, 6, 30),
                     acceptance_datetime=datetime(2026, 8, 14, tzinfo=timezone.utc),
                     holdings=[Holding13F(cusip="037833100", issuer="APPLE INC", value_usd=1e9)])
    cur = Filing13F(filer_cik="0001067983", filer_name="Berkshire Hathaway",
                    period_end=date(2026, 9, 30),
                    acceptance_datetime=datetime(2026, 11, 16, tzinfo=timezone.utc),
                    holdings=[Holding13F(cusip="037833100", issuer="APPLE INC", value_usd=1.5e9),
                              Holding13F(cusip="594918104", issuer="MICROSOFT CORP", value_usd=4e8)])
    payload = radar_payload([prev, cur], DECISION, watchlist={"AAPL", "MSFT"},
                            issuer_to_ticker={"Apple Inc.": "AAPL", "Microsoft Corp": "MSFT"})
    rows = {r["issuer"]: r for r in payload["filers"][0]["changes"]}
    assert rows["APPLE INC"]["ticker"] == "AAPL" and rows["APPLE INC"]["in_watchlist"] is True
    assert rows["MICROSOFT CORP"]["ticker"] == "MSFT" and rows["MICROSOFT CORP"]["in_watchlist"] is True
    assert payload["watchlist_overlap"] == ["AAPL", "MSFT"]
    assert payload["counts"]["increase"] == 1 and payload["counts"]["new"] == 1


def test_radar_without_index_keeps_legacy_behaviour():
    filing = Filing13F(filer_cik="0001067983", filer_name="Berkshire Hathaway",
                       period_end=date(2026, 9, 30),
                       acceptance_datetime=datetime(2026, 11, 16, tzinfo=timezone.utc),
                       holdings=[Holding13F(cusip="594918104", issuer="MICROSOFT CORP", value_usd=1e8)])
    payload = radar_payload([filing], DECISION, watchlist={"MSFT"})
    assert payload["watchlist_overlap"] == []                   # 색인 없으면 이름이 바로 티커일 때만
    labelled = radar_payload([filing], DECISION, watchlist={"MICROSOFT CORP"})
    assert labelled["watchlist_overlap"] == ["MICROSOFT CORP"]


def test_radar_marks_cusip_map_first():
    filing = Filing13F(filer_cik="0001067983", filer_name="Berkshire Hathaway",
                       period_end=date(2026, 9, 30),
                       acceptance_datetime=datetime(2026, 11, 16, tzinfo=timezone.utc),
                       holdings=[Holding13F(cusip="037833100", issuer="APPLE INC", value_usd=1e8)])
    payload = radar_payload([filing], DECISION, watchlist={"AAPL"},
                            cusip_to_ticker={"037833100": "AAPL"})
    assert payload["filers"][0]["changes"][0]["ticker"] == "AAPL"
    assert payload["watchlist_overlap"] == ["AAPL"]


def test_load_company_tickers_uses_cache(tmp_path):
    cache = tmp_path / "sec_cik" / "company_tickers.json"
    client = FakeSecClient(tickers=[{"ticker": "AAPL", "cik": 320193, "title": "Apple Inc."}])
    first = load_company_tickers(client, cache_path=cache)
    second = load_company_tickers(client, cache_path=cache)
    assert first == second and client.ticker_calls == 1
    assert build_issuer_ticker_index(second)["APPLE"] == ["AAPL"]


# ────────────────────────────── 공개 범위 ──────────────────────────────
def test_radar_output_passes_commit_guard(tmp_path):
    """레이더 JSON 에 금지 키(`shares`·`holdings`·`avg_cost`)가 없다 — 공개 커밋 가능."""
    client = _berkshire_client()
    collected = collect_13f_filings(DECISION, filers=[_berkshire_filer()], client=client)
    radar = radar_payload(collected["filings"], DECISION, watchlist={"AAPL"},
                          issuer_to_ticker={"Apple Inc.": "AAPL"})
    path = tmp_path / "13f.json"
    path.write_text(json.dumps(radar, ensure_ascii=False, indent=2), encoding="utf-8")
    rules = check_commit_scope.load_rules(ROOT / "config" / "public_allowlist.yaml")
    assert check_commit_scope.check_paths([str(path)], rules, root=tmp_path) == []
    text = json.dumps(radar, ensure_ascii=False)
    for key in ('"shares"', '"avg_cost"', '"holdings"'):
        assert key not in text


def test_parse_filing_dict_path_still_works():
    filing = parse_filing({"filer_cik": "1067983", "period_end": "2026-09-30",
                           "acceptance_datetime": "2026-11-16T21:00:04Z",
                           "holdings": [{"cusip": "037833100", "issuer": "APPLE INC", "value_usd": 1e8}]})
    assert filing.filer_cik == "0001067983"
    assert filing.as_dict()["n_holdings"] == 1


# ────────────────────────────── run_screen 배선 ──────────────────────────────
@pytest.fixture
def screen_root(tmp_path):
    """`--root` 샌드박스 — config/params 와 워치리스트만 복사한다(저장소 파일을 건드리지 않는다)."""
    (tmp_path / "config").mkdir()
    shutil.copy(ROOT / "config" / "params_v001.yaml", tmp_path / "config" / "params_v001.yaml")
    shutil.copy(ROOT / "config" / "13f_filers.yaml", tmp_path / "config" / "13f_filers.yaml")
    (tmp_path / "data" / "watchlist").mkdir(parents=True)
    (tmp_path / "data" / "radar").mkdir(parents=True)
    (tmp_path / "data" / "watchlist" / "current.json").write_text(json.dumps({
        "schema_version": 1, "as_of": "2026-11-20",
        "watchlist": [{"ticker": "AAPL", "sector": "Information Technology"},
                      {"ticker": "MSFT", "sector": "Information Technology"}],
    }, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def _collected() -> dict:
    client = _berkshire_client()
    return collect_13f_filings(DECISION, filers=[_berkshire_filer()], client=client)


def _run(argv: list[str]) -> tuple[int, str]:
    """`main()` 을 돌리고 cwd 를 되돌린다 — run_screen 은 `--root` 로 chdir 한다."""
    import contextlib
    import io

    previous = Path.cwd()
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            code = run_screen.main(argv)
    finally:
        os.chdir(previous)
    return code, buffer.getvalue()


def test_run_screen_13f_only_writes_radar_and_keeps_watchlist(screen_root, monkeypatch):
    watch_before = (screen_root / "data" / "watchlist" / "current.json").read_text(encoding="utf-8")
    monkeypatch.setattr(run_screen, "collect_13f_filings", lambda *a, **k: _collected())
    monkeypatch.setattr(run_screen, "_issuer_ticker_index", lambda: {"APPLE": ["AAPL"]})
    code, out = _run(["--13f-only", "--as-of", "2026-11-21", "--root", str(screen_root)])
    assert code == 0
    radar = json.loads((screen_root / "data" / "radar" / "13f.json").read_text(encoding="utf-8"))
    assert radar["watermark_ko"] == "45일 전 분기말 기준"
    assert radar["period_end"] == "2026-09-30"
    assert radar["n_filers"] == 1 and radar["status"] == "ok"
    assert radar["watchlist_overlap"] == ["AAPL"]
    assert radar["filers"][0]["filer_cik"] == "0001067983"
    assert (screen_root / "data" / "watchlist" / "current.json").read_text(encoding="utf-8") == watch_before
    summary = json.loads(out.strip().splitlines()[-1])
    assert summary["mode"] == "13f-only" and summary["n_filers"] == 1


def test_run_screen_13f_only_survives_collector_crash(screen_root, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("SEC 503")

    monkeypatch.setattr(run_screen, "collect_13f_filings", _boom)
    monkeypatch.setattr(run_screen, "_issuer_ticker_index", lambda: {})
    code, out = _run(["--13f-only", "--as-of", "2026-11-21", "--root", str(screen_root)])
    assert code == 0
    radar = json.loads((screen_root / "data" / "radar" / "13f.json").read_text(encoding="utf-8"))
    assert radar["watermark_ko"] == "45일 전 분기말 기준"
    assert radar["filers"] == [] and radar["status"] == "failed"
    assert "13F 수집 실패" in radar["note_ko"]
    summary = json.loads(out.strip().splitlines()[-1])
    assert summary["status"] == "failed" and summary["errors"]


def test_run_screen_full_live_auto_collects(screen_root, monkeypatch):
    from engine.data import fixtures

    monkeypatch.setattr(run_screen, "load_dataset",
                        lambda args, as_of, decision_at: fixtures.dataset(as_of))
    monkeypatch.setattr(run_screen, "collect_13f_filings", lambda *a, **k: _collected())
    monkeypatch.setattr(run_screen, "_issuer_ticker_index", lambda: {"APPLE": ["AAPL"]})
    code, out = _run(["--as-of", "2026-11-20", "--root", str(screen_root)])
    assert code == 0
    radar = json.loads((screen_root / "data" / "radar" / "13f.json").read_text(encoding="utf-8"))
    assert radar["n_filers"] == 1, "라이브 모드는 --13f 없이도 13F 를 채워야 한다"
    assert radar["filers"][0]["n_holdings"] == 3
    assert json.loads(out.strip().splitlines()[-1])["13f"]["status"] == "ok"


def test_run_screen_fixtures_mode_does_not_collect(screen_root, monkeypatch):
    from engine.data import fixtures

    calls: list[str] = []

    def _forbidden(*args, **kwargs):
        calls.append("called")
        raise AssertionError("fixtures 모드에서는 SEC 를 부르지 않는다")

    monkeypatch.setattr(run_screen, "load_dataset",
                        lambda args, as_of, decision_at: fixtures.dataset(as_of))
    monkeypatch.setattr(run_screen, "collect_13f_filings", _forbidden)
    code, _ = _run(["--fixtures", "--as-of", "2026-11-20", "--root", str(screen_root)])
    assert code == 0 and calls == []
    radar = json.loads((screen_root / "data" / "radar" / "13f.json").read_text(encoding="utf-8"))
    assert radar["n_filers"] == 0 and radar["watermark_ko"] == "45일 전 분기말 기준"


def test_run_screen_keeps_previous_radar_when_collection_fails(screen_root, monkeypatch):
    from engine.data import fixtures

    good = radar_payload(_collected()["filings"], DECISION, watchlist={"AAPL"})
    (screen_root / "data" / "radar" / "13f.json").write_text(
        json.dumps(good, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(run_screen, "load_dataset",
                        lambda args, as_of, decision_at: fixtures.dataset(as_of))
    monkeypatch.setattr(run_screen, "collect_13f_filings", lambda *a, **k: {
        "filings": [], "status": "missing_key", "errors": [{"stage": "config", "error": "SEC_USER_AGENT 없음"}],
        "skipped": [], "n_filers": 0, "n_filings": 0, "note_ko": "SEC_USER_AGENT 없음 — 13F 미수집"})
    monkeypatch.setattr(run_screen, "_issuer_ticker_index", lambda: {})
    code, _ = _run(["--as-of", "2026-11-21", "--root", str(screen_root)])
    assert code == 0
    radar = json.loads((screen_root / "data" / "radar" / "13f.json").read_text(encoding="utf-8"))
    assert radar["n_filers"] == 1, "일시 실패로 지난 레이더를 지우지 않는다"
    assert radar["filers"][0]["filer_cik"] == "0001067983"
    assert radar["as_of"] == "2026-11-21"
    assert "지난 레이더 유지" in radar["note_ko"]
    assert radar["stale_days"] == 52                     # 2026-09-30 → 2026-11-21
