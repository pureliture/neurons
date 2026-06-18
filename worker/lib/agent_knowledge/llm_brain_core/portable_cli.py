from __future__ import annotations

import argparse
import json

from .portable import export_llm_brain_archive, import_llm_brain_archive


def export_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge brain-export")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--repo-root", default="")
    args = parser.parse_args(argv)
    report = export_llm_brain_archive(
        ledger_path=args.ledger,
        output_path=args.out,
        repo_root=args.repo_root or None,
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0


def import_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="neuron-knowledge brain-import")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--archive", required=True)
    args = parser.parse_args(argv)
    report = import_llm_brain_archive(
        ledger_path=args.ledger,
        archive_path=args.archive,
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0
