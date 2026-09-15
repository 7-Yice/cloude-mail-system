#!/usr/bin/env python3
"""Resolve one correspondent and render an on-demand correspondence view."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from mail_identities import IdentityError
from send_mail import (
    DEFAULT_OMBRE_ENV,
    DEFAULT_OMBRE_MCP_URL,
    _mcp_tool_json,
    read_env,
)


DEFAULT_PROFILES = Path(
    os.environ.get(
        "CLOUDE_MAIL_PROFILES_DIR",
        str(Path.home() / ".config" / "cloude-mail-system" / "mail-profiles"),
    )
)


def _mcp(
    *, env_path: Path, url: str, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    values = read_env(env_path)
    return _mcp_tool_json(
        url=url,
        token=str(values.get("OMBRE_MCP_TOKEN") or ""),
        name=name,
        arguments=arguments,
    )


def resolve_constellation(
    recipient: str, *, env_path: Path, url: str
) -> tuple[str, dict[str, Any]]:
    value = str(recipient or "").strip()
    if not value:
        raise IdentityError("recipient name or constellation id is required")
    if value.startswith("cst_"):
        inspected = _mcp(
            env_path=env_path,
            url=url,
            name="constellation_read",
            arguments={
                "action": "inspect",
                "constellation_id": value,
                "max_tokens": 800,
            },
        )
        return value, inspected

    pointers = _mcp(
        env_path=env_path,
        url=url,
        name="constellation_read",
        arguments={"action": "pointers", "query": value, "max_tokens": 800},
    )
    exact: list[tuple[str, dict[str, Any]]] = []
    for pointer in pointers.get("pointers") or []:
        constellation_id = str(pointer.get("constellation_id") or "")
        inspected = _mcp(
            env_path=env_path,
            url=url,
            name="constellation_read",
            arguments={
                "action": "inspect",
                "constellation_id": constellation_id,
                "max_tokens": 800,
            },
        )
        business = inspected.get("business") or {}
        names = [business.get("name"), *(business.get("aliases") or [])]
        variants = [str(name or "").strip() for name in names]
        variants.extend(
            part.strip()
            for name in names
            for part in re.split(r"[/／]", str(name or ""))
            if part.strip()
        )
        if any(name.casefold() == value.casefold() for name in variants):
            exact.append((constellation_id, inspected))
    if not exact:
        raise IdentityError(
            f"no exact person constellation name or alias matched {value!r}"
        )
    if len(exact) != 1:
        raise IdentityError(f"recipient name or alias is ambiguous: {value!r}")
    return exact[0]


def _profile_path(
    root: Path, constellation_id: str, canonical_name: str
) -> tuple[Path | None, Path]:
    if root.is_dir():
        matches = []
        for path in sorted(root.glob("*.md")):
            try:
                if constellation_id in path.read_text(encoding="utf-8"):
                    matches.append(path)
            except (OSError, UnicodeError):
                continue
        if len(matches) == 1:
            return matches[0], matches[0]
    slug = re.sub(r"[^a-z0-9]+", "-", canonical_name.casefold()).strip("-")
    suggested = root / f"{slug or constellation_id}.md"
    return None, suggested


def render(prep: dict[str, Any], *, profile_root: Path) -> str:
    recipient = prep.get("recipient") or {}
    canonical = str(recipient.get("canonical_name") or "")
    aliases = recipient.get("aliases") or []
    found, suggested = _profile_path(
        profile_root, str(prep.get("constellation_id") or ""), canonical
    )
    lines = [
        "【收件人确认卡】",
        f"姓名：{canonical}",
        f"别名：{' / '.join(str(value) for value in aliases) if aliases else '无'}",
        f"星座：{prep.get('constellation_id')}",
        f"状态：星座 {recipient.get('constellation_status')} / 通信身份 {recipient.get('identity_status')}",
        f"门牌：{recipient.get('masked_address')}",
        "",
        "【本地人物卡】",
    ]
    if found is None:
        lines.append(f"尚未建立；建议路径：{suggested}")
    else:
        lines.extend([f"路径：{found}", found.read_text(encoding="utf-8").strip()])
    for key, title in (("incoming", "对方最近来信"), ("sent", "我方最近发信")):
        lines.extend(["", f"【{title}】"])
        rows = prep.get(key) or []
        if not rows:
            lines.append("无")
            continue
        for index, row in enumerate(rows, 1):
            summary = str(row.get("summary") or "").strip()
            summary_text = summary or "摘要暂不可用；需要时展开原文。"
            lines.extend(
                [
                    f"{index}. {row.get('date')}｜{row.get('subject')}",
                    f"摘要：{summary_text}",
                    f"原文：prep_mail.py {prep.get('constellation_id')} --original {row.get('archive_id')}",
                ]
            )
    return "\n".join(lines).rstrip() + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="写信前摊平收件人、人物卡与最近往来。"
    )
    parser.add_argument("recipient", help="人物星座 ID、规范名或精确别名")
    parser.add_argument("--count", type=int, default=2)
    parser.add_argument("--original", default="", metavar="ARCHIVE_ID")
    parser.add_argument("--cursor", type=int, default=0)
    parser.add_argument("--max-tokens", type=int, default=3000)
    parser.add_argument("--profiles", type=Path, default=DEFAULT_PROFILES)
    parser.add_argument("--ombre-env", type=Path, default=DEFAULT_OMBRE_ENV)
    parser.add_argument("--ombre-url", default=DEFAULT_OMBRE_MCP_URL)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        constellation_id, _ = resolve_constellation(
            args.recipient, env_path=args.ombre_env, url=args.ombre_url
        )
        arguments = {
            "constellation_id": constellation_id,
            "view": "original" if args.original else "ledger",
            "count": args.count,
            "archive_id": args.original,
            "cursor": args.cursor,
            "max_tokens": args.max_tokens,
        }
        prep = _mcp(
            env_path=args.ombre_env,
            url=args.ombre_url,
            name="prepare_mail_reply",
            arguments=arguments,
        )
        if args.json:
            output = json.dumps(prep, ensure_ascii=False, indent=2, sort_keys=True)
        elif args.original:
            output = str(prep.get("content") or "")
            next_cursor = int((prep.get("page") or {}).get("next_cursor") or 0)
            if next_cursor:
                output += (
                    "\n\n【原文未完】继续：prep_mail.py "
                    f"{constellation_id} --original {args.original} --cursor {next_cursor}"
                )
        else:
            output = render(prep, profile_root=args.profiles)
        print(output)
        return 0
    except (OSError, UnicodeError, ValueError, IdentityError) as exc:
        print(
            json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
