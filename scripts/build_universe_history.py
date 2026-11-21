#!/usr/bin/env python3
"""scripts/build_universe_history.py — S&P 500 시점별 구성종목 이력(`data/universe_history.csv`) 빌더.

[왜 필요한가]
스트레스 구간(2022/2020/2018Q4)은 수집 시작 전이라 **그때의 구성종목**을 따로 알아야 한다.
오늘의 유니버스로 과거를 돌리면 살아남은 종목만 남아 성과가 좋게 나온다(생존편향, 기획서 2.4.5).

[무엇을 만드나]
`engine.data.universe.UNIVERSE_COLUMNS` 규격의 CSV — 한 행이 **한 구성원 구간**이다.

    date,ticker,index_name,sector,added_at,removed_at,announcement_at,effective_at

- `effective_at` = 그 종목이 지수에 **편입된 날**(발효일). `removed_at` 은 편출일(현재 구성원은 빈 값).
- `announcement_at` 은 공개 소스에 없으므로 **비운다**(발표일을 지어내지 않는다).
- `sector` 는 현재 구성종목 표의 **최신 업종**이다 — 그 시점 업종이 아니다(그 사실을 리포트에 남긴다).

[안전핀 — 절대 규칙]
무료 공개 소스(Wikipedia)로는 **상장폐지·피합병 종목의 최종가와 상장폐지 수익률
(`delisting_return`)** 을 완전히 확보할 수 없다. 그래서 이 빌더는 유니버스 이력만 만들고,
`delisted_securities` 는 **만들지 않는다**(`--write-delisted-stub` 로 '불완전' 표본만 만들 수 있다).
상장폐지 데이터가 완전하지 않은 동안 `stress_report` 는 게이트를 통과로 계산하지 않는다.

[쓰는 법]

    python scripts/build_universe_history.py                     # 네트워크에서 받아 생성
    python scripts/build_universe_history.py --offline-html a.html --changes-html b.html
    python scripts/build_universe_history.py --out data/universe_history.csv --json-report out/universe_build.json

종료 코드: 0 성공 · 2 검증 실패 · 3 입력 없음(네트워크·오프라인 HTML 모두 없음)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Iterable
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.backtest.pipeline_runner import verify_universe_inputs  # noqa: E402
from engine.backtest.stress import STRESS_WINDOWS  # noqa: E402
from engine.data.universe import (UNIVERSE_COLUMNS, load_delisted, membership_at,  # noqa: E402
                                  validate_universe)

DEFAULT_CONSTITUENTS_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
DEFAULT_CHANGES_URL = "https://en.wikipedia.org/wiki/Historical_components_of_the_S%26P_500"
DEFAULT_OUT = Path("data/universe_history.csv")
USER_AGENT = "us-stock-advisor/0.1 (universe history builder; research use)"
INDEX_NAME = "SP500"
#: 지금은 없는 종목의 업종을 채울 때 쓰는 값 — 모르는 값을 지어내지 않는다.
UNKNOWN_SECTOR = ""

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


# ────────────────────────────── HTML 표 파서(순수 함수) ──────────────────────────────
def _cell_text(cell) -> str:
    """셀 텍스트 — 각주(sup) 표식과 정규화 공백을 제거한다."""
    for footnote in cell.find_all("sup"):
        footnote.decompose()
    return re.sub(r"\s+", " ", cell.get_text(" ", strip=True)).strip()


def table_grid(table) -> list[list[str]]:
    """HTML 표 → 2차원 문자열 격자(`rowspan`·`colspan` 을 펼친다).

    `pandas.read_html` 없이 순수 파서로 두는 이유: 오프라인 픽스처 테스트가 작은 HTML 로 돌고,
    날짜 묶음(rowspan) 안에서 **어느 행에 어떤 값이 붙는지**를 우리가 직접 통제하기 위해서다.
    """
    rows = table.find_all("tr")
    grid: list[list[str]] = []
    pending: dict[tuple[int, int], str] = {}
    for r, row in enumerate(rows):
        cells = row.find_all(["th", "td"])
        width = max((len(cells) + sum(1 for (rr, _cc) in pending if rr == r), 1))
        line: list[str] = [""] * width
        column = 0
        for cell in cells:
            while (r, column) in pending:
                line[column] = pending.pop((r, column))
                column += 1
            text = _cell_text(cell)
            span = int(cell.get("colspan") or 1)
            rowspan = int(cell.get("rowspan") or 1)
            for offset in range(span):
                if column + offset >= len(line):
                    line.append("")
                line[column + offset] = text
                for extra in range(1, rowspan):
                    pending[(r + extra, column + offset)] = text
            column += span
        while (r, column) in pending:
            if column >= len(line):
                line.append("")
            line[column] = pending.pop((r, column))
            column += 1
        grid.append(line)
    return grid


def _find_tables(html: str):
    from bs4 import BeautifulSoup  # noqa: PLC0415 — 파서 라이브러리는 이 함수 안에서만 쓴다

    soup = BeautifulSoup(html, "lxml")
    return soup.find_all("table")


def _header_index(header: list[str], *names: str) -> int | None:
    lowered = [h.lower() for h in header]
    for name in names:
        for i, value in enumerate(lowered):
            if name.lower() in value:
                return i
    return None


def _norm_ticker(value: str) -> str:
    text = str(value or "").strip().upper().replace(" ", "")
    return text if _TICKER_RE.match(text) else ""


def _parse_date(value: str) -> date | None:
    stamp = pd.to_datetime(str(value or "").strip(), errors="coerce")
    if stamp is pd.NaT or pd.isna(stamp):
        return None
    return stamp.date()


def parse_constituents_html(html: str) -> list[dict]:
    """현재 구성종목 표 → `[{ticker, sector, date_added}]`.

    표를 못 찾으면 **빈 목록**(0건을 정상으로 꾸미지 않는다 — 호출자가 실패로 처리한다).
    """
    out: list[dict] = []
    for table in _find_tables(html):
        grid = table_grid(table)
        if len(grid) < 2:
            continue
        header = grid[0]
        symbol = _header_index(header, "Symbol", "Ticker")
        sector = _header_index(header, "GICS Sector", "Sector")
        added = _header_index(header, "Date added", "Date Added")
        if symbol is None:
            continue
        for row in grid[1:]:
            if symbol >= len(row):
                continue
            ticker = _norm_ticker(row[symbol])
            if not ticker:
                continue
            out.append({
                "ticker": ticker,
                "sector": row[sector] if sector is not None and sector < len(row) else UNKNOWN_SECTOR,
                "date_added": row[added] if added is not None and added < len(row) else "",
            })
    return out


def parse_changes_html(html: str) -> list[dict]:
    """구성종목 변경 이력 표 → `[{effective_date, added, removed, reason}]`.

    `rowspan` 으로 날짜가 묶인 표(같은 날 여러 건)도 격자 확장으로 각 행에 날짜가 붙는다.
    """
    out: list[dict] = []
    for table in _find_tables(html):
        grid = table_grid(table)
        if not grid:
            continue
        # 2단 헤더(날짜 / Added·Removed) 도 있으므로 앞 3줄에서 열 위치를 찾는다.
        header_row = None
        columns: dict[str, int] = {}
        for i, row in enumerate(grid[:3]):
            eff = _header_index(row, "Effective Date", "Date")
            added = next((j for j, v in enumerate(row) if "added" in v.lower()), None)
            removed = next((j for j, v in enumerate(row) if "removed" in v.lower()), None)
            if eff is not None and (added is not None or removed is not None):
                header_row = i
                columns = {"date": eff, "added": added, "removed": removed}
                break
        if header_row is None:
            continue
        ticker_cols: dict[str, int] = {}
        for name in ("added", "removed"):
            col = columns.get(name)
            if col is None:
                continue
            for j in range(col, min(col + 2, len(grid[header_row]))):
                if "ticker" in grid[header_row][j].lower():
                    ticker_cols[name] = j
                    break
            else:
                ticker_cols[name] = col
        for row in grid[header_row + 1:]:
            eff = _parse_date(row[columns["date"]]) if columns["date"] < len(row) else None
            if eff is None:
                continue
            added = row[ticker_cols["added"]] if "added" in ticker_cols and ticker_cols["added"] < len(row) else ""
            removed = (row[ticker_cols["removed"]]
                       if "removed" in ticker_cols and ticker_cols["removed"] < len(row) else "")
            out.append({
                "effective_date": eff.isoformat(),
                "added": _norm_ticker(added),
                "removed": _norm_ticker(removed),
                "reason": row[-1] if len(row) > 2 else "",
            })
    return out


# ────────────────────────────── 이력 재구성 ──────────────────────────────
def build_universe_rows(constituents: Iterable[dict], changes: Iterable[dict],
                        renames: Iterable[dict] | None = None) -> tuple[list[dict], dict]:
    """현재 구성종목 + 변경 이력 → `UNIVERSE_COLUMNS` 행 목록.

    각 종목의 편입·편출 이벤트를 시간순으로 접어 **구간별 행**을 만든다(재편입도 별도 구간).

    [정직성 규칙]
    - 시작일을 모르는 구간은 만들지 않고 `unknown_start` 로 센다 — 추정 날짜를 지어내지 않는다.
    - **편출 기록을 찾지 못한 종목**(티커 변경·이름 변경으로 편출 행의 티커가 달라진 경우)은
      현재 구성종목이 아니면 그 구간을 **버리고** `unresolved_removals` 로 센다 — 없는 편출일을
      지어내지도, 아직 구성원이라고 우기지도 않는다. `renames`(티커 변경표)를 주면 그 구간이 메워진다.
    """
    sectors = {row["ticker"]: (row.get("sector") or UNKNOWN_SECTOR) for row in constituents}
    current = {row["ticker"] for row in constituents}
    events: dict[str, list[tuple[date, int]]] = {}
    unknown_start: set[str] = set()
    date_added_from_table: set[str] = set()
    rename_rows = list(renames or [])
    for row in constituents:
        ticker = row["ticker"]
        added = _parse_date(row.get("date_added", ""))
        if added is None:
            unknown_start.add(ticker)
            continue
        date_added_from_table.add(ticker)
        events.setdefault(ticker, []).append((added, +1))
    for row in changes:
        day = _parse_date(row.get("effective_date", ""))
        if day is None:
            continue
        if row.get("added"):
            events.setdefault(row["added"], []).append((day, +1))
        if row.get("removed"):
            events.setdefault(row["removed"], []).append((day, -1))
    for row in rename_rows:
        day = _parse_date(str(row.get("date") or ""))
        if day is None:
            continue
        if row.get("from"):
            events.setdefault(_norm_ticker(row["from"]) or str(row["from"]), []).append((day, -1))
        if row.get("to"):
            events.setdefault(_norm_ticker(row["to"]) or str(row["to"]), []).append((day, +1))
    rows: list[dict] = []
    unresolved: list[str] = []
    for ticker, items in events.items():
        active_since: date | None = None
        for day, delta in sorted(items):
            if delta > 0 and active_since is None:
                active_since = day
            elif delta < 0 and active_since is not None:
                if day > active_since:
                    rows.append(_row(ticker, sectors.get(ticker, UNKNOWN_SECTOR), active_since, day))
                active_since = None
        if active_since is not None:
            if ticker in current:
                rows.append(_row(ticker, sectors.get(ticker, UNKNOWN_SECTOR), active_since, None))
            else:
                unresolved.append(ticker)
    rows.sort(key=lambda r: (r["effective_at"], r["ticker"], r["removed_at"] or "9999"))
    stats = {
        "tickers": len(events),
        "intervals": len(rows),
        "unknown_start": sorted(unknown_start)[:40],
        "n_unknown_start": len(unknown_start),
        "unresolved_removals": sorted(unresolved)[:40],
        "n_unresolved_removals": len(unresolved),
        "date_added_from_constituents": len(date_added_from_table),
        "changes_rows": len(list(changes)),
        "rename_rows": len(rename_rows),
    }
    return rows, stats


def _row(ticker: str, sector: str, added: date, removed: date | None) -> dict:
    return {
        "date": added.isoformat(),
        "ticker": ticker,
        "index_name": INDEX_NAME,
        "sector": sector,
        "added_at": added.isoformat(),
        "removed_at": removed.isoformat() if removed else "",
        "announcement_at": "",
        "effective_at": added.isoformat(),
    }


def to_frame(rows: Iterable[dict]) -> pd.DataFrame:
    """행 목록 → 계약 컬럼 순서의 DataFrame(빈 removed_at 은 결측)."""
    frame = pd.DataFrame(list(rows), columns=UNIVERSE_COLUMNS)
    for column in ("added_at", "removed_at", "announcement_at", "effective_at"):
        frame[column] = pd.to_datetime(frame[column], errors="coerce")
    return frame


# ────────────────────────────── 입출력 ──────────────────────────────
def fetch_html(url: str, timeout: int = 45) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 — 공개 https URL
        raw = response.read()
    return raw.decode("utf-8", errors="replace")


def read_html_source(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def load_rename_map(path: str | Path) -> list[dict]:
    """티커 변경표(`from,to,date`) — 있으면 편출입 이력의 틈을 메운다(없으면 빈 목록).

    무료 소스의 변경 이력 표는 **티커가 바뀐 종목**의 편출 행을 새 티커로 적는다
    (예: KORS → CPRI 이후 편출). 그래서 변경표가 없으면 그 구간의 편출일을 알 수 없다.
    """
    frame = pd.read_csv(path) if Path(path).exists() else pd.DataFrame()
    if frame.empty:
        return []
    return [{"from": row.get("from"), "to": row.get("to"), "date": row.get("date")}
            for row in frame.to_dict("records")]


def build(out_path: str | Path = DEFAULT_OUT, *, constituents_html: str | None = None,
          changes_html: str | None = None, constituents_url: str = DEFAULT_CONSTITUENTS_URL,
          changes_url: str = DEFAULT_CHANGES_URL, write_delisted_stub: bool = False,
          delisted_path: str | Path = "data/delisted_securities.csv",
          rename_map: str | Path | None = None) -> dict:
    """이력 빌드 1회 — 반환값이 그대로 리포트(JSON)가 된다."""
    report: dict = {"out": str(out_path), "source": {}, "status": None, "reasons": []}
    constituents_text = constituents_html if constituents_html is not None else _try_fetch(
        constituents_url, report, "constituents")
    changes_text = changes_html if changes_html is not None else _try_fetch(changes_url, report, "changes")
    if not constituents_text:
        report["status"] = "no_input"
        report["reasons"].append("constituents_html_missing")
        return report
    constituents = parse_constituents_html(constituents_text)
    changes = parse_changes_html(changes_text) if changes_text else []
    report["source"] = {"constituents_url": None if constituents_html else constituents_url,
                        "changes_url": None if changes_html or not changes_text else changes_url,
                        "n_constituents": len(constituents), "n_changes": len(changes)}
    if not constituents:
        report["status"] = "no_input"
        report["reasons"].append("constituents_parse_empty")
        return report
    if not changes:
        report["reasons"].append("changes_parse_empty — 편출입 이력 없이 현재 구성종목만 반영된다")
    rows, stats = build_universe_rows(constituents, changes,
                                      load_rename_map(rename_map) if rename_map else None)
    report["stats"] = stats
    frame = to_frame(rows)
    report["validation"] = validate_universe(frame)
    report["coverage"] = {
        "start": (frame["effective_at"].min().date().isoformat()
                  if not frame.empty and frame["effective_at"].notna().any() else None),
        "end": (frame["effective_at"].max().date().isoformat()
                if not frame.empty and frame["effective_at"].notna().any() else None),
        "current_members": int(frame["removed_at"].isna().sum()) if not frame.empty else 0,
        "membership_at_windows": _membership_counts(frame),
        "note_ko": ("구성종목 수가 지수 규모(500)보다 적은 구간은 공개 변경 이력이 선별돼 있어 "
                    "빠진 구간이 있다는 뜻이다 — 그런 이력은 게이트에 쓸 수 없다."),
    }
    if report["validation"].get("ok"):
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(out, index=False, date_format="%Y-%m-%d")
        report["written"] = str(out)
        report["status"] = "ok"
    else:
        report["status"] = "invalid"
        report["reasons"].append(f"validate_universe_failed:{report['validation'].get('reasons')}")
    delisted = Path(delisted_path)
    if write_delisted_stub and report["status"] == "ok":
        removed = frame.loc[frame["removed_at"].notna(), "ticker"].drop_duplicates()
        stub = pd.DataFrame({"ticker": removed, "cik": None, "delist_date": None,
                             "delist_type": "unknown", "final_price": None,
                             "delisting_return": None, "source": "wikipedia_changes_only"})
        delisted.parent.mkdir(parents=True, exist_ok=True)
        stub.to_csv(delisted, index=False)
        (delisted.parent / "delisted_securities.meta.json").write_text(json.dumps(
            {"complete": False, "coverage_start": None, "source": "wikipedia_changes_only",
             "note_ko": "상장폐지 최종가·수익률 미확보 — 게이트에 쓸 수 없다(불완전 표본)."},
            ensure_ascii=False, indent=2), encoding="utf-8")
        report["delisted_stub"] = str(delisted)
    report["delisted"] = _delisted_state(Path(delisted_path))
    report["gate_note_ko"] = (
        "universe_history 만으로는 게이트를 통과시키지 않는다 — delisted_securities 가 "
        "완전하지 않으면 stress_report 는 verified=False·gate_passed=False 를 유지한다(2.4.5)."
    )
    return report


def _membership_counts(frame: pd.DataFrame) -> dict:
    """스트레스 구간 시작일의 구성종목 수 — 이력의 완전성을 드러내는 값."""
    from datetime import datetime, timezone  # noqa: PLC0415

    out: dict[str, int] = {}
    for window in STRESS_WINDOWS:
        stamp = datetime.combine(window["start"], datetime.min.time(), tzinfo=timezone.utc)
        out[window["start"].isoformat()] = len(membership_at(frame, stamp))
    return out


def _try_fetch(url: str, report: dict, key: str) -> str | None:
    try:
        text = fetch_html(url)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        report["reasons"].append(f"fetch_failed:{key}:{type(exc).__name__}:{str(exc)[:80]}")
        return None
    report.setdefault("fetched", {})[key] = len(text)
    return text


def _delisted_state(path: Path) -> dict:
    """상장폐지 데이터 확보 상태 — **없으면 없다고** 남긴다."""
    if not path.exists():
        return {"path": str(path), "exists": False, "complete": False,
                "reason": "file_missing — 무료 API 로는 상장폐지 최종가·수익률을 완전히 확보할 수 없다"}
    loading = load_delisted(str(path))
    verdict = verify_universe_inputs(None, loading, delisted_complete=False)
    return {"path": str(path), "exists": True, "rows": int(len(loading)),
            "complete": verdict["delisted_verified"], "reasons": verdict["reasons"]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="S&P 500 시점별 구성종목 이력 빌더")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--offline-html", default=None, help="현재 구성종목 표 HTML 파일(오프라인 테스트)")
    ap.add_argument("--changes-html", default=None, help="변경 이력 표 HTML 파일(오프라인 테스트)")
    ap.add_argument("--constituents-url", default=DEFAULT_CONSTITUENTS_URL)
    ap.add_argument("--changes-url", default=DEFAULT_CHANGES_URL)
    ap.add_argument("--json-report", default=None, help="리포트 JSON 저장 경로")
    ap.add_argument("--write-delisted-stub", action="store_true",
                    help="편출 종목만 담은 '불완전' 상장폐지 표본을 쓴다(게이트 사용 불가)")
    ap.add_argument("--delisted", default="data/delisted_securities.csv")
    ap.add_argument("--rename-map", default="data/universe_renames.csv",
                    help="티커 변경표(from,to,date) — 있으면 편출입 이력의 틈을 메운다")
    args = ap.parse_args(argv)
    constituents_html = read_html_source(args.offline_html) if args.offline_html else None
    changes_html = read_html_source(args.changes_html) if args.changes_html else None
    report = build(args.out, constituents_html=constituents_html, changes_html=changes_html,
                   constituents_url=args.constituents_url, changes_url=args.changes_url,
                   write_delisted_stub=args.write_delisted_stub, delisted_path=args.delisted,
                   rename_map=args.rename_map)
    if args.json_report:
        path = Path(args.json_report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    if report["status"] == "ok":
        return 0
    if report["status"] == "no_input":
        return 3
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
