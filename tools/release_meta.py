#!/usr/bin/env python3
"""独立版本线的 Release / CI 元数据计算。

本仓库（hoey94/mfw-licence）独立维护，不再同步上游，也不读取任何 Release API；
版本基线只来自仓库内的 ``app/common/__version__.py``。

规则：
- 正式 tag：``vMAJOR.MINOR.PATCH``，可选 ``-alpha.N`` / ``-beta.N`` / ``-rc.N``
- 非 tag 构建：``v<基线>-ci.<YYMMDD>.<7 位短 SHA>``
- 预发布：tag 含 ``-alpha`` / ``-beta`` / ``-rc`` / ``-ci``
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import os
import re
import sys
from pathlib import Path

RELEASE_TAG_RE = re.compile(r"v\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)\.\d+)?")
BASE_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")
PRERELEASE_RE = re.compile(r"-(?:alpha|beta|rc|ci)")


def read_base_version(version_file: Path) -> str:
    """从 app/common/__version__.py 读取基线版本，去掉 v 前缀与预发布后缀。"""
    tree = ast.parse(version_file.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        ):
            continue
        raw = str(ast.literal_eval(node.value))
        base = raw.lstrip("v").split("-")[0]
        if not BASE_VERSION_RE.fullmatch(base):
            raise ValueError(f"invalid base version in {version_file}: {raw!r}")
        return base
    raise ValueError(f"__version__ not found in {version_file}")


def compute_meta(
    ref: str, sha: str, base_version: str, now: dt.datetime
) -> dict[str, str]:
    if ref.startswith("refs/tags/"):
        tag = ref[len("refs/tags/") :]
        if not RELEASE_TAG_RE.fullmatch(tag):
            raise ValueError(
                f"invalid release tag {tag!r}; expected vMAJOR.MINOR.PATCH"
                " with optional -alpha.N/-beta.N/-rc.N"
            )
        is_release = "true"
    else:
        is_release = "false"
        short_sha = (sha or "unknown")[:7]
        tag = f"v{base_version}-ci.{now:%y%m%d}.{short_sha}"

    return {
        "tag": tag,
        "is_release": is_release,
        "is_prerelease": "true" if PRERELEASE_RE.search(tag) else "false",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default=os.environ.get("GITHUB_REF", ""))
    parser.add_argument("--sha", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument(
        "--version-file",
        type=Path,
        default=Path("app/common/__version__.py"),
    )
    parser.add_argument("--date", help="ISO date (YYYY-MM-DD), for local dry runs")
    parser.add_argument("--output", type=Path, help="override GITHUB_OUTPUT path")
    args = parser.parse_args(argv)

    try:
        base_version = read_base_version(args.version_file)
        now = (
            dt.datetime.fromisoformat(args.date).replace(tzinfo=dt.timezone.utc)
            if args.date
            else dt.datetime.now(dt.timezone.utc)
        )
        meta = compute_meta(args.ref, args.sha, base_version, now)
    except (OSError, ValueError, SyntaxError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    lines = [f"{key}={value}" for key, value in meta.items()]
    print("\n".join(lines))

    output = args.output or os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
