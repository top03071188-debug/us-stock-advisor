#!/usr/bin/env python3
"""scripts/archive_snapshot.py — 비공개 아카이브용 스냅샷 패키징·해시 검증·업로드.

`gh release upload --skip-existing` 은 **존재하지 않는 플래그**다(gh 2.102.0 확인).
그래서 업로드는 다음 순서로만 한다(기획서 4.9 / 6.3 / v1.3.1):

  1) 스냅샷을 `snapshot_{T}_{snapshot_id_short}.tgz` 로 패키징
  2) `gh release view <tag> --repo <archive> --json assets` 로 자산 목록 조회
  3) 같은 이름이 있으면 로컬 tgz sha256 과 대조
       - 일치 → 스킵(정상, append-only)
       - 불일치 → **에러 중단**(재생성·변조된 원본)
     없으면 → `--clobber` 없이 `gh release upload`

서브커맨드:
  package      --out DIR --name NAME [--source DIR]
  check-asset  --assets assets.json --file TGZ [--repo R --tag T]
  upload       --repo R --tag T --file TGZ
종료 코드(check-asset): 0=존재·일치, 10=없음(업로드 필요), 1=불일치/오류
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def package(source: str | Path, out_dir: str | Path, name: str) -> dict:
    """디렉터리를 tgz 로 묶고 `.sha256` 파일을 함께 남긴다.

    manifest.json 이 있으면 그 안의 snapshot_id 를 확인해 파일명과 대조한다.
    """
    source = Path(source)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        raise FileNotFoundError(source)
    tgz = out_dir / f"{name}.tgz"
    # 결정론적 tar: 정렬된 경로, 고정 mtime/소유자
    with tarfile.open(tgz, "w:gz") as tar:
        for p in sorted(source.rglob("*")):
            if p.is_file():
                info = tar.gettarinfo(str(p), arcname=str(p.relative_to(source.parent)))
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                with open(p, "rb") as fh:
                    tar.addfile(info, fh)
    digest = sha256_file(tgz)
    (out_dir / f"{name}.sha256").write_text(f"{digest}  {tgz.name}\n", encoding="utf-8")
    return {"file": str(tgz), "sha256": digest, "size": tgz.stat().st_size}


@dataclass
class CheckResult:
    code: int
    reason: str
    asset_name: str
    local_sha256: str
    asset_sha256: str | None = None


def _collect_assets(payload) -> list[dict]:
    if isinstance(payload, dict):
        assets = payload.get("assets", payload)
        if isinstance(assets, dict):
            assets = list(assets.values())
        return list(assets or [])
    return list(payload or [])


def check_asset(assets_payload, tgz: str | Path, *, repo: str | None = None, tag: str | None = None,
                runner=subprocess.run, download_dir: str | Path | None = None) -> CheckResult:
    """자산 목록에서 같은 이름을 찾아 sha256 을 대조한다."""
    tgz = Path(tgz)
    name = tgz.name
    local = sha256_file(tgz)
    assets = _collect_assets(assets_payload)
    match = next((a for a in assets if str(a.get("name")) == name), None)
    if match is None:
        return CheckResult(10, "ASSET_ABSENT", name, local)

    asset_digest = match.get("digest") or match.get("sha256")
    if asset_digest:
        asset_digest = str(asset_digest).replace("sha256:", "").strip()
        if asset_digest == local:
            return CheckResult(0, "ASSET_HASH_MATCH", name, local, asset_digest)
        return CheckResult(1, "ASSET_HASH_MISMATCH", name, local, asset_digest)

    # digest 가 없는 gh 버전: 내려받아 직접 해시한다.
    if not (repo and tag):
        return CheckResult(1, "ASSET_DIGEST_MISSING_NO_DOWNLOAD", name, local)
    tmp = Path(download_dir or tempfile.mkdtemp(prefix="archive_dl_"))
    proc = runner(
        ["gh", "release", "download", tag, "--repo", repo, "--pattern", name, "--dir", str(tmp), "--clobber"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return CheckResult(1, f"DOWNLOAD_FAILED:{proc.stderr.strip()[:200]}", name, local)
    downloaded = tmp / name
    if not downloaded.exists():
        return CheckResult(1, "DOWNLOAD_MISSING_FILE", name, local)
    remote = sha256_file(downloaded)
    code = 0 if remote == local else 1
    return CheckResult(code, "ASSET_HASH_MATCH" if code == 0 else "ASSET_HASH_MISMATCH", name, local, remote)


def upload(repo: str, tag: str, tgz: str | Path, runner=subprocess.run) -> dict:
    """`--clobber` 없이 업로드한다(있으면 실패 → check-asset 단계가 먼저 걸러야 한다)."""
    cmd = ["gh", "release", "upload", tag, str(tgz), "--repo", repo]
    proc = runner(cmd, capture_output=True, text=True)
    return {"ok": proc.returncode == 0, "cmd": cmd, "stderr": proc.stderr.strip()[:300]}


def ensure_release(repo: str, tag: str, runner=subprocess.run) -> dict:
    """릴리스가 없으면 만든다(있으면 그대로)."""
    view = runner(["gh", "release", "view", tag, "--repo", repo, "--json", "assets"], capture_output=True, text=True)
    if view.returncode == 0:
        return {"created": False, "assets": json.loads(view.stdout or "{}").get("assets", [])}
    create = runner(
        ["gh", "release", "create", tag, "--repo", repo, "--title", f"Snapshots {tag}",
         "--notes", "walk-forward 재현용 원본 (비공개, append-only)"],
        capture_output=True, text=True,
    )
    return {"created": create.returncode == 0, "stderr": create.stderr.strip()[:300], "assets": []}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="비공개 아카이브 스냅샷 패키징·해시 검증")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("package")
    p.add_argument("--source", default="data/snapshots")
    p.add_argument("--out", default="dist_archive")
    p.add_argument("--name", required=True)
    p.add_argument("--json", action="store_true")

    c = sub.add_parser("check-asset")
    c.add_argument("--assets", required=True)
    c.add_argument("--file", required=True)
    c.add_argument("--repo")
    c.add_argument("--tag")

    u = sub.add_parser("upload")
    u.add_argument("--repo", required=True)
    u.add_argument("--tag", required=True)
    u.add_argument("--file", required=True)

    args = ap.parse_args(argv)
    if args.cmd == "package":
        result = package(args.source, args.out, args.name)
        print(json.dumps(result, ensure_ascii=False) if args.json else result["file"])
        return 0
    if args.cmd == "check-asset":
        payload = json.loads(Path(args.assets).read_text(encoding="utf-8"))
        res = check_asset(payload, args.file, repo=args.repo, tag=args.tag)
        print(json.dumps({"code": res.code, "reason": res.reason, "asset": res.asset_name,
                          "local_sha256": res.local_sha256, "asset_sha256": res.asset_sha256}, ensure_ascii=False))
        return res.code
    if args.cmd == "upload":
        res = upload(args.repo, args.tag, args.file)
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res["ok"] else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
