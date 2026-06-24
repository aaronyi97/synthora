#!/usr/bin/env python3
"""运行时生成 AUDIT_CONTEXT_ANCHOR。

输出为 Markdown，可直接贴入外部审计词；默认写 stdout，也可用 --output 输出到临时文件。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "dist",
    "__pycache__",
    "data",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}


def run_git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build runtime AUDIT_CONTEXT_ANCHOR markdown")
    parser.add_argument(
        "--repo",
        default=".",
        help="Target git repository root (default: current working directory)",
    )
    parser.add_argument(
        "--scope-in",
        action="append",
        default=[],
        help="In-scope boundary; repeatable",
    )
    parser.add_argument(
        "--scope-out",
        action="append",
        default=[],
        help="Out-of-scope boundary; repeatable",
    )
    parser.add_argument(
        "--attachment",
        action="append",
        default=[],
        help="Attachment path or note; repeatable",
    )
    parser.add_argument(
        "--baseline",
        default="HEAD",
        help="Git baseline/ref for CHANGED_FILES_SINCE_BASELINE (default: HEAD)",
    )
    parser.add_argument(
        "--output",
        help="Optional output markdown path; omit to print to stdout",
    )
    return parser.parse_args()


def build_tree_lines(root: Path) -> list[str]:
    lines: list[str] = [f"{root.name}/"]

    def walk(path: Path, prefix: str) -> None:
        children = sorted(
            (child for child in path.iterdir() if child.name not in EXCLUDED_DIRS),
            key=lambda child: (child.is_file(), child.name.lower(), child.name),
        )
        for index, child in enumerate(children):
            connector = "└── " if index == len(children) - 1 else "├── "
            suffix = "/" if child.is_dir() else ""
            lines.append(f"{prefix}{connector}{child.name}{suffix}")
            if child.is_dir():
                next_prefix = prefix + ("    " if index == len(children) - 1 else "│   ")
                walk(child, next_prefix)

    walk(root, "")
    return lines


def bullet_block(values: list[str], empty_note: str) -> str:
    if not values:
        return f"- {empty_note}"
    return "\n".join(f"- {item}" for item in values)


def build_markdown(
    repo: Path,
    branch: str,
    head: str,
    worktree_status: str,
    uncommitted_files: list[str],
    scope_in: list[str],
    scope_out: list[str],
    attachments: list[str],
    changed_files: list[str],
    baseline: str,
) -> str:
    captured_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    tree = "\n".join(build_tree_lines(repo))
    return f"""## AUDIT_CONTEXT_ANCHOR

- TARGET_REPO: `{repo}`
- TARGET_BRANCH: `{branch}`
- TARGET_HEAD: `{head}`
- CAPTURED_AT: `{captured_at}`
- WORKTREE_STATUS: `{worktree_status}`

### UNCOMMITTED_FILES
{bullet_block(uncommitted_files, "(clean worktree)")}

### SCOPE_IN
{bullet_block(scope_in, "(must be provided by caller)")}

### SCOPE_OUT
{bullet_block(scope_out, "(must be provided by caller)")}

### DIRECTORY_SNAPSHOT
```text
{tree}
```

### ATTACHMENT_MANIFEST
{bullet_block(attachments, "(must be provided by caller)")}

### CHANGED_FILES_SINCE_BASELINE
- BASELINE: `{baseline}`
{bullet_block(changed_files, "(no tracked file changes vs baseline)")}
"""


def main() -> int:
    args = parse_args()
    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists():
        print(f"Not a git repository: {repo}", file=sys.stderr)
        return 1

    branch = run_git(repo, "branch", "--show-current")
    head = run_git(repo, "rev-parse", "HEAD")
    status_output = run_git(repo, "status", "--short")
    uncommitted_files = status_output.splitlines() if status_output else []
    worktree_status = "DIRTY" if uncommitted_files else "CLEAN"
    changed_output = run_git(repo, "diff", "--name-only", f"{args.baseline}..HEAD")
    changed_files = changed_output.splitlines() if changed_output else []

    markdown = build_markdown(
        repo=repo,
        branch=branch,
        head=head,
        worktree_status=worktree_status,
        uncommitted_files=uncommitted_files,
        scope_in=args.scope_in,
        scope_out=args.scope_out,
        attachments=args.attachment,
        changed_files=changed_files,
        baseline=args.baseline,
    )

    if args.output:
        output_path = Path(args.output).resolve()
        output_path.write_text(markdown, encoding="utf-8")
    else:
        sys.stdout.write(markdown)
        if not markdown.endswith("\n"):
            sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
