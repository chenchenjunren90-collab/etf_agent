"""打包可运行程序到桌面 ZIP（≤30MB）：仅源码、配置、数据，无说明文档/生成器。"""
from __future__ import annotations

import os
import zipfile
from datetime import datetime
from pathlib import Path

SRC = Path(__file__).resolve().parent
DESKTOP = Path.home() / "Desktop"
STAMP = datetime.now().strftime("%Y%m%d")
ZIP_NAME = f"ETF日内配置智能体_可运行源码_{STAMP}.zip"
ZIP_PATH = DESKTOP / ZIP_NAME
MAX_ZIP_MB = 30.0

ARCHIVE_ROOT = "etf_agent"
DEMO_DATE = "2026-06-12"

SKIP_DIRS = {
    ".git", ".cursor", "__pycache__", ".venv", "venv", "env",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".idea", ".vscode",
    "archive",
}

# 非运行文件：文档、生成器、打包/清理脚本
SKIP_FILES = {
    ".env", ".env.local", ".gitignore",
    "pack_submission.py", "cleanup_project.py", "build_log.txt",
    "gen_manual_docx.py", "gen_manual_audit_docx.py", "gen_figure_prompts_docx.py",
    "README.md", "STRATEGY_DOC.md",
    "回测方案_数据源与参数优化.md", "参数来源与数据分级说明.md",
    "说明书附录_回测与参数说明.md", "评委说明_决策逻辑与评分规则.md",
    "项目汇报_新闻驱动ETF日内策略.md",
    "data/_manual_review.txt",
}

SKIP_SUFFIXES = (".pyc", ".pyo", ".log")

SKIP_PATH_PREFIXES = (
    "data/historical_news_signal/",
    "data/news_backtest/historical_news_signal/",
    "data/news_backtest/signals/",
)

# 运行必需的非 .py 文件（其余 .md/.txt 文档一律不进包）
RUNTIME_ALLOWLIST = {
    "requirements.txt",
    ".env.example",
    "dashboard.html",
    "start_dashboard.bat",
    "start_agent.bat",
    "start_auto.bat",
    "setup_task.ps1",
    "auto_theme_signal.json",
    "prompts/decider_zh.md",  # 大模型决策提示词，非说明文档
}


def _keep_daily_output(rel: Path) -> bool:
    return rel.name == "last_pnl_report.txt" or rel.name.startswith(DEMO_DATE)


def _keep_daily_news_signal(rel: Path) -> bool:
    return rel.name == f"{DEMO_DATE}.json"


def should_skip(rel: Path) -> bool:
    posix = rel.as_posix()
    parts = rel.parts

    if any(p in SKIP_DIRS for p in parts):
        return True
    for pref in SKIP_PATH_PREFIXES:
        if posix.startswith(pref):
            return True
    if rel.name in SKIP_FILES:
        return True
    if rel.name.endswith(SKIP_SUFFIXES):
        return True

    # 根目录及子目录中的说明类 markdown 全部排除（仅保留 prompts/decider_zh.md）
    if rel.suffix.lower() == ".md" and posix not in RUNTIME_ALLOWLIST:
        return True

    if len(parts) >= 2 and parts[0] == "data" and parts[1] == "daily_output":
        return not _keep_daily_output(rel)
    if len(parts) >= 2 and parts[0] == "data" and parts[1] == "daily_news_signal":
        return not _keep_daily_news_signal(rel)

    # 根目录只允许 .py + RUNTIME_ALLOWLIST
    if len(parts) == 1:
        if rel.suffix.lower() == ".py":
            return False
        return posix not in RUNTIME_ALLOWLIST

    return False


def iter_files() -> list[tuple[Path, Path]]:
    pairs: list[tuple[Path, Path]] = []
    for dirpath, dirnames, filenames in os.walk(SRC):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        base = Path(dirpath)
        for fn in filenames:
            full = base / fn
            rel = full.relative_to(SRC)
            if should_skip(rel):
                continue
            pairs.append((full, Path(ARCHIVE_ROOT) / rel))
    return pairs


def main() -> Path:
    files = iter_files()
    if not files:
        raise SystemExit("没有可打包文件")

    for old in DESKTOP.glob("ETF日内配置智能体_可运行源码_*.zip"):
        old.unlink()

    total_bytes = 0
    with zipfile.ZipFile(ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for src, arc in sorted(files, key=lambda x: str(x[1]).lower()):
            zf.write(src, arc.as_posix())
            total_bytes += src.stat().st_size

    zip_mb = ZIP_PATH.stat().st_size / 1024 / 1024
    print(f"已生成: {ZIP_PATH}")
    print(f"文件数: {len(files)}")
    print(f"原始体积约: {total_bytes / 1024 / 1024:.1f} MB")
    print(f"ZIP 体积约: {zip_mb:.2f} MB")

    # 自检：不得含文档/生成器
    bad_suffix = {".md", ".docx", ".doc"}
    bad_names = {"提交说明.txt", "README.md"}
    with zipfile.ZipFile(ZIP_PATH) as zf:
        for name in zf.namelist():
            base = Path(name).name
            if base in bad_names or base.startswith("gen_"):
                raise SystemExit(f"包内仍含非程序文件: {name}")
            if Path(name).suffix.lower() in bad_suffix and name != f"{ARCHIVE_ROOT}/prompts/decider_zh.md":
                raise SystemExit(f"包内仍含文档: {name}")

    if zip_mb > MAX_ZIP_MB:
        raise SystemExit(f"ZIP 超过 {MAX_ZIP_MB} MB 限制。")
    return ZIP_PATH


if __name__ == "__main__":
    main()
