#!/usr/bin/env python3
"""Build the 毛选人格 skill from the full local 毛泽东选集 dump."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.maoxuan.corpus import load_corpus
from scripts.maoxuan.features import aggregate_features, pick_representatives
from scripts.maoxuan.skill import (
    TARGET_NAME,
    compile_meta,
    compile_persona_md,
    compile_skill_prompt,
    compile_work_md,
    dump_json,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Distill 毛选人格 from a local corpus")
    parser.add_argument(
        "--corpus",
        required=True,
        help="Local 毛泽东选集_全 directory",
    )
    parser.add_argument(
        "--out",
        default=str(ROOT / "skills" / "maoxuan"),
        help="Output directory for the skill bundle",
    )
    args = parser.parse_args()
    corpus_root = Path(args.corpus)
    if not corpus_root.is_dir():
        raise SystemExit(f"corpus not found: {corpus_root}")
    articles = load_corpus(corpus_root)
    if not articles:
        raise SystemExit("no articles loaded")
    stats = aggregate_features(articles)
    representatives = pick_representatives(articles)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "work.md").write_text(compile_work_md(stats, representatives), encoding="utf-8")
    (out / "persona.md").write_text(compile_persona_md(stats), encoding="utf-8")
    (out / "SKILL.md").write_text(
        "---\n"
        f"name: colleague-maoxuan\n"
        f'description: "{TARGET_NAME} — 基于毛泽东选集正编与补编分层蒸馏"\n'
        "user-invocable: true\n"
        "---\n\n"
        + compile_skill_prompt(stats)
        + "\n",
        encoding="utf-8",
    )
    dump_json(out / "meta.json", compile_meta(stats, str(corpus_root)))
    dump_json(out / "representatives.json", {"items": representatives})
    slim = dict(stats)
    slim.pop("rows", None)
    dump_json(out / "corpus_stats.json", slim)
    with (out / "corpus_index.jsonl").open("w", encoding="utf-8") as handle:
        for article in articles:
            handle.write(json.dumps(article.to_index_row(), ensure_ascii=False) + "\n")
    print(
        json.dumps(
            {
                "out": str(out),
                "articles": stats["article_count"],
                "by_source": stats["by_source"],
                "by_period": {key: value["articles"] for key, value in stats["by_period"].items()},
                "skill_chars": len(compile_skill_prompt(stats)),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
