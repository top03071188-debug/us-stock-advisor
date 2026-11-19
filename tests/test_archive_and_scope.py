"""test_archive_and_scope — 아카이브 업로드(해시 검증)와 공개 범위 가드 (2.2.3, 4.9, 6.3)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import archive_snapshot as arch  # noqa: E402
import build_public_data as bpd  # noqa: E402
import check_commit_scope as ccs  # noqa: E402
import redact_quotes as rq  # noqa: E402


# ── archive_snapshot ────────────────────────────────────────────────────
def test_package_produces_tgz_and_sha(tmp_path):
    src = tmp_path / "snap_2026-11-19_abc123"
    src.mkdir()
    (src / "manifest.json").write_text('{"snapshot_id":"sha256:x"}', encoding="utf-8")
    ( src / "prices.csv").write_text("date,raw_close\n2026-11-18,101\n", encoding="utf-8")
    out = arch.package(src, tmp_path / "dist", "snapshot_2026-11-19_abc123")
    assert Path(out["file"]).exists()
    assert len(out["sha256"]) == 64
    side = tmp_path / "dist" / "snapshot_2026-11-19_abc123.sha256"
    assert side.read_text().startswith(out["sha256"])


def test_check_asset_exit_codes(tmp_path):
    tgz = tmp_path / "snapshot_2026-11-19_abc123.tgz"
    tgz.write_bytes(b"payload")
    digest = arch.sha256_file(tgz)

    present = {"assets": [{"name": tgz.name, "size": tgz.stat().st_size, "digest": f"sha256:{digest}"}]}
    assert arch.check_asset(present, tgz).code == 0

    # 같은 이름인데 해시가 다르면 **중단**해야 한다(원본 변조·재생성)
    mismatch = {"assets": [{"name": tgz.name, "digest": "sha256:" + "0" * 64}]}
    res = arch.check_asset(mismatch, tgz)
    assert res.code == 1 and res.reason == "ASSET_HASH_MISMATCH"

    absent = {"assets": [{"name": "other.tgz", "digest": "sha256:" + "1" * 64}]}
    assert arch.check_asset(absent, tgz).code == 10


def test_check_asset_without_digest_downloads_and_hashes(tmp_path):
    tgz = tmp_path / "snapshot_2026-11-19_abc123.tgz"
    tgz.write_bytes(b"payload")
    calls = []

    def fake_runner(cmd, **kw):
        calls.append(cmd)
        # gh release download ... --dir <dir> — 그 폴더에 같은 내용을 둔다
        d = Path(cmd[cmd.index("--dir") + 1])
        d.mkdir(parents=True, exist_ok=True)
        (d / tgz.name).write_bytes(b"payload")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    assets = {"assets": [{"name": tgz.name, "size": 7}]}
    res = arch.check_asset(assets, tgz, repo="o/r", tag="snapshots-2026", runner=fake_runner, download_dir=tmp_path / "dl")
    assert res.code == 0 and calls and "download" in calls[0]


def test_upload_never_uses_clobber(tmp_path):
    tgz = tmp_path / "a.tgz"
    tgz.write_bytes(b"x")
    captured = {}

    def fake_runner(cmd, **kw):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "", "")

    res = arch.upload("owner/archive", "snapshots-2026", tgz, runner=fake_runner)
    assert res["ok"] is True
    assert "--clobber" not in captured["cmd"]
    assert "--skip-existing" not in captured["cmd"]      # 존재하지 않는 플래그
    assert captured["cmd"][:3] == ["gh", "release", "upload"]


def test_yaml_uses_view_assets_and_no_skip_existing():
    wf = (ROOT / ".github" / "workflows" / "daily-postclose.yml").read_text(encoding="utf-8")
    upload_lines = [ln.split("#", 1)[0] for ln in wf.splitlines()
                    if "gh release upload" in ln and not ln.strip().startswith("#")]
    assert upload_lines and all("--skip-existing" not in ln and "--clobber" not in ln for ln in upload_lines)
    assert "gh release view" in wf and "--json assets" in wf
    assert "check-asset" in wf


# ── redact ──────────────────────────────────────────────────────────────
def test_redact_quotes_replaces_text_with_hash(tmp_path):
    payload = {"signals": [{"ticker": "MSFT", "evidence": [
        {"type": "news", "quote": "we now expect Azure growth of 18%", "url": "https://x"},
    ]}]}
    f = tmp_path / "q.json"
    f.write_text(json.dumps(payload), encoding="utf-8")
    rq.redact_file(f)
    out = json.loads(f.read_text(encoding="utf-8"))
    ev = out["signals"][0]["evidence"][0]
    assert "quote" not in ev
    assert len(ev["quote_sha256"]) == 64 and ev["quote_chars"] > 0
    # 같은 문장은 같은 해시(정규화)
    assert rq.quote_digest("  we  now expect ") == rq.quote_digest("we now expect")


def test_redact_jsonl(tmp_path):
    f = tmp_path / "records.jsonl"
    f.write_text(json.dumps({"signal_id": "s1", "body_excerpt_1500": "long text"}) + "\n", encoding="utf-8")
    rq.redact_file(f)
    rec = json.loads(f.read_text(encoding="utf-8").splitlines()[0])
    assert "body_excerpt_1500" not in rec and rec["body_chars"] == 9


# ── public data ─────────────────────────────────────────────────────────
def test_build_public_data_filters_and_blocks_forbidden(tmp_path):
    rules = bpd.load_allowlist(ROOT / "config" / "public_allowlist.yaml")
    signals = {
        "as_of": "2026-11-19", "params_version": "params_v001", "snapshot_id": "sha256:abc",
        "signals": [{"ticker": "MSFT", "type": "BUY", "entry_score": 69.4, "thesis_ko": "요약",
                     "raw_close": 412.35, "avg_cost": 400.0, "evidence": [{"url": "https://x", "quote": "원문"}]}],
        "watchlist": [{"ticker": "MSFT", "q": 82, "v": 71, "g": 60}],
    }
    out = bpd.build(signals, None, None, rules)
    today = out["signals/today.json"]
    sig = today["signals"][0]
    assert sig["entry_score"] == 69.4 and sig["thesis_ko"] == "요약"
    assert "raw_close" not in sig and "avg_cost" not in sig
    assert "quote" not in sig["evidence"][0]
    assert "raw_close" not in json.dumps(out, ensure_ascii=False)


def test_build_public_data_fails_loudly_on_deny_key():
    rules = bpd.load_allowlist(ROOT / "config" / "public_allowlist.yaml")
    with pytest.raises(SystemExit):
        bpd.assert_no_forbidden({"signals": [{"holdings": [1, 2]}]}, rules)


# ── commit scope ────────────────────────────────────────────────────────
def test_commit_scope_flags_secret_files_and_paths(tmp_path):
    rules = ccs.load_rules(ROOT / "config" / "public_allowlist.yaml")
    env = tmp_path / ".env"
    env.write_text("FRED_API_KEY=secret", encoding="utf-8")
    jsonf = tmp_path / "signals.json"
    jsonf.write_text(json.dumps({"signals": [{"quote": "hello"}]}), encoding="utf-8")
    problems = ccs.check_paths([str(env), str(jsonf), "data/raw/prices/AAPL.parquet"], rules)
    text = "\n".join(problems)
    assert "secret file" in text
    assert "forbidden keys" in text
    assert "forbidden path" in text


def test_commit_scope_clean_file_passes(tmp_path):
    rules = ccs.load_rules(ROOT / "config" / "public_allowlist.yaml")
    ok = tmp_path / "quant_signals.json"
    ok.write_text(json.dumps({"signals": [{"ticker": "MSFT", "entry_score": 69.4,
                                           "quote_sha256": "a" * 64, "quote_verified": True}]}), encoding="utf-8")
    assert ccs.check_paths([str(ok)], rules) == []


def test_allowlist_declares_required_denials():
    rules = yaml.safe_load((ROOT / "config" / "public_allowlist.yaml").read_text(encoding="utf-8"))
    for key in ("quote", "body_excerpt_1500", "avg_cost", "holdings", "raw_close"):
        assert key in rules["deny_keys"]
    assert "data/raw/" in rules["deny_paths"]
    assert ".env" in rules["deny_paths"]


def test_package_is_deterministic(tmp_path):
    """같은 스냅샷을 다시 패키징해도 sha256 이 같아야 '해시 일치 → 스킵'이 성립한다."""
    src = tmp_path / "snap_2026-11-19_abc123"
    src.mkdir()
    (src / "manifest.json").write_text('{"snapshot_id":"sha256:x"}', encoding="utf-8")
    (src / "prices.csv").write_text("date,raw_close\n2026-11-18,101\n", encoding="utf-8")
    a = arch.package(src, tmp_path / "d1", "snap")
    b = arch.package(src, tmp_path / "d2", "snap")
    assert a["sha256"] == b["sha256"]
    # 같은 이름·같은 해시면 check-asset 은 0(스킵)을 돌려준다
    payload = {"assets": [{"name": "snap.tgz", "digest": "sha256:" + a["sha256"]}]}
    assert arch.check_asset(payload, a["file"]).code == 0
