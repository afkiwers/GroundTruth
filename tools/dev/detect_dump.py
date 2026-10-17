"""Everything detection finds on a board (nets, converters, bridges: every spec field, reason and model), as JSON:
refactoring detection must not change it. Run it on main in a `git worktree` for the baseline, then on the change,
and compare the two files (`fc /b` or `cmp`).

    .venv/Scripts/python tools/dev/detect_dump.py OUT.json BOARD [BOARD ...]

Uses a fresh in-memory settings file per board (nothing is written next to a board).
"""
from __future__ import annotations

import dataclasses
import json
import sys
import tempfile
from pathlib import Path


def dump(board_path: str) -> dict:
    """Everything detection finds on one board, with a fresh settings file in a temporary folder (so the result
    depends on the board only)."""
    from groundtruth.adapters.file_source import load_board
    from groundtruth.analysis.detect import detect_bridges, detect_converters, detect_nets, power_nets
    from groundtruth.config.settings import SettingsFile
    board = load_board(board_path)
    with tempfile.TemporaryDirectory() as tmp:
        sc = SettingsFile.new(Path(tmp) / "x.groundtruth.yaml", board)
        cfg = sc.config
        conv = detect_converters(board, cfg)
        brid = detect_bridges(board, cfg)
        specs = [d.spec for d in conv] + [s for d in brid for s in d.spec.stages()]
        nets = detect_nets(board, cfg, 0, skip=power_nets(board, cfg, specs))

    def plain(d):
        """A detection result as plain data: its spec, confidence, reasons and suggested part models."""
        return {"spec": dataclasses.asdict(d.spec), "confidence": d.confidence, "reasons": d.reasons,
                "models": {r: m.to_dict() for r, m in sorted(getattr(d, "models", {}).items())}}
    return {"converters": [plain(d) for d in conv], "bridges": [plain(d) for d in brid],
            "nets": [{"spec": dataclasses.asdict(d.spec), "confidence": d.confidence, "reasons": d.reasons}
                     for d in nets]}


def main(argv: list[str]) -> int:
    """The command line: OUT.json BOARD [BOARD ...]; writes the JSON (sorted keys, so two runs compare with cmp) and
    prints a count per board."""
    out, boards = Path(argv[0]), argv[1:]
    result = {Path(b).name: dump(b) for b in boards}
    out.write_text(json.dumps(result, indent=1, sort_keys=True, default=str), encoding="utf-8")
    print(f"{out}: " + ", ".join(f"{k}: {len(v['converters'])} conv, {len(v['bridges'])} bridges, "
                                 f"{len(v['nets'])} nets" for k, v in result.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
