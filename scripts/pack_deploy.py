"""最小部署包（服务器运行所需），保留目录结构。
用法: python scripts/pack_deploy.py   → 产出 da-deploy.zip
"""
from __future__ import annotations

import os
import zipfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "da-deploy.zip")

EXCLUDE_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__",
                ".pytest_cache", ".mypy_cache", ".ruff_cache", "data",
                "research", ".pnpm-store", ".dsh", "screenshots", "ui_screens",
                "docs", "dist"}
EXCLUDE_FILES = {".env", ".env.local"}
KEEP_TOP = {"src", "config", "infra", "scripts", "web", "tests",
            "requirements.txt", ".env.example", ".dockerignore", "Makefile",
            "README.md", "start_local.bat"}


def main() -> int:
    files: list[tuple[str, str]] = []  # (abs, rel)
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            if fn in EXCLUDE_FILES:
                continue
            abs_p = os.path.join(dirpath, fn)
            rel = os.path.relpath(abs_p, ROOT).replace("\\", "/")
            parts = rel.split("/")
            if parts[0] == "dividend-alpha-v3-handoff":
                if rel != "dividend-alpha-v3-handoff/web/data.json":
                    continue
            elif parts[0] not in KEEP_TOP:
                continue
            files.append((abs_p, rel))

    n = 0
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for abs_p, rel in files:
            z.write(abs_p, rel)
            n += 1
    print(f"已打包: {OUT}  ({os.path.getsize(OUT) // 1024} KB, {n} 文件)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
