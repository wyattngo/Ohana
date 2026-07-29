#!/usr/bin/env python3
"""roadmap-dashboard — MỘT dashboard hợp nhất cho toàn lộ trình.

Gộp ba thứ trước đây rời nhau (audit 2026-07-23):
  · `adp-dashboard`          → coverage L1xL2xL3 + sức khoẻ spine
  · `adp-progress-dashboard` → click phase xem checkpoint đầy đủ
  · `roadmap_derive tree`    → xương sống Phase → Step(gate) → Work item → Task

Cây là xương sống; click bất kỳ nút nào để đọc nội dung thật của nó:
  Step      → Target + Tests của gate (docs/gates/)
  Work item → acceptance ở ROADMAP §4.1 + Class + derives_from
  Task      → nguyên phase block ADP (GOAL/GATE/EVIDENCE/REVIEW/SMOKE/RISK)

    python3 scripts/roadmap_dashboard.py            # → docs/roadmap-dashboard.html
    python3 scripts/roadmap_dashboard.py --out X    # đích khác

Stdlib ONLY — chạy được trên `python3` hệ thống, đúng interpreter CI dùng.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import roadmap_derive as rd  # noqa: E402

FIELD_RE = re.compile(r"^([A-Z_]+):\s*(.*)$")
TARGET_RE = re.compile(r"^-\s+(.*)$", re.M)


def _section(text: str, head: str, nxt: str) -> str:
    if head not in text:
        return ""
    body = text.split(head, 1)[1]
    return body.split(nxt, 1)[0] if nxt and nxt in body else body


def collect(root: Path) -> dict:
    declared, entries = rd.read_roadmap(root)
    gates = rd.parse_gates(root)
    tasks = rd.parse_tasks(root)
    lines = (root / rd.ROADMAP_REL).read_text(encoding="utf-8").splitlines()

    # §4.1 — Class + nội dung + acceptance cho mỗi ID.
    meta: dict[str, dict] = {}
    for line in rd._slice(lines, rd.SEC_41, rd.SEC_411):
        m = rd.ID_ROW_RE.match(line)
        if not m:
            continue
        cols = [c.strip().replace("\\|", "|") for c in rd.PIPE_SPLIT_RE.split(line)]
        meta[m.group(1)] = {
            "what": cols[2] if len(cols) > 2 else "",
            "acceptance": cols[3] if len(cols) > 3 else "",
            "cls": cols[4].strip("* ") if len(cols) > 4 else "?",
            "waits": cols[5] if len(cols) > 5 else "",
        }

    # docs/gates — Target + Test policy đầy đủ (DEC-OHANA-06: policy là hợp đồng
    # bất biến, không state; L2 spec sinh JIT sẽ tiêu thụ mỗi câu thành GATE:).
    gmeta: dict[str, dict] = {}
    gdir = root / rd.GATES_REL
    for g in gates:
        f = gdir / f"{g.gate_id}.md"
        raw = f.read_text(encoding="utf-8") if f.is_file() else ""
        gmeta[g.gate_id] = {
            "title": (re.search(r"^# .*?— (.*)$", raw, re.M) or [None, ""])[1],
            "targets": TARGET_RE.findall(_section(raw, "## Target", "## Test policy")),
            "policy": [
                t.strip()
                for t in re.findall(
                    r"^-\s+(.+)$", _section(raw, "## Test policy", "## Bound"), re.M
                )
            ],
        }

    # docs/tasks — nguyên phase block cho drawer.
    pmeta: dict[str, dict] = {}
    tdir = root / "docs" / "tasks"
    for f in sorted(tdir.glob("*.md")) if tdir.is_dir() else []:
        spec = f.name.split("-")[0]
        raw = f.read_text(encoding="utf-8")
        title = (re.search(r"^#\s+(.*)$", raw, re.M) or [None, f.stem])[1]
        for chunk in raw.split("<!-- ADP:PHASE")[1:]:
            pid = chunk.split("-->", 1)[0].strip()
            block = chunk.split("<!-- /ADP", 1)[0].split("-->", 1)[-1]
            fields: dict[str, str] = {}
            cur = None
            for ln in block.splitlines():
                fm = FIELD_RE.match(ln)
                if fm:
                    cur = fm.group(1)
                    fields[cur] = fm.group(2)
                elif cur and ln.strip():
                    fields[cur] += " " + ln.strip()
            pmeta[f"{spec}:{pid}"] = {"spec": spec, "phase": pid, "spec_title": title, "f": fields}

    by_anchor: dict[str, list] = {}
    for e in entries:
        key = e.ref[1] if e.ref else ("(scaffold)" if e.is_scaffold else "(?)")
        by_anchor.setdefault(key, []).append(e)

    def items_for(anchor: str) -> list[dict]:
        out = []
        for e in by_anchor.get(anchor, []):
            ph = tasks.get(e.work_id, [])
            out.append(
                {
                    "id": e.work_id,
                    "weak": e.is_weak,
                    "status": rd.rollup(ph),
                    "phases": [f"{s}:{p}" for s, p, _ in ph],
                    "phase_status": {f"{s}:{p}": st for s, p, st in ph},
                    **meta.get(e.work_id, {}),
                    "derives": e.raw,
                }
            )
        return out

    steps = []
    drift = []
    for g in gates:
        its = items_for(g.anchor)
        if set(g.bound_declared) != {i["id"] for i in its}:
            drift.append(
                f"{g.gate_id}: gate khai {sorted(g.bound_declared)} ≠ §4.1.1 "
                f"{sorted(i['id'] for i in its)}"
            )
        steps.append(
            {
                "id": g.gate_id,
                "anchor": g.anchor,
                "signed": g.signed,
                "by": g.approved_by,
                "items": its,
                **gmeta.get(g.gate_id, {}),
            }
        )

    outside = [
        {"anchor": k, "items": items_for(k)}
        for k in sorted(by_anchor)
        if k not in {g.anchor for g in gates}
    ]

    dangling = []
    for e in entries:
        if e.is_scaffold or not e.ref:
            continue
        av = rd.resolve_layer(root, e.ref[0])
        if av is None or e.ref[1] not in av:
            dangling.append(f"{e.work_id} → {e.raw}")

    internal = [i for i in meta.values() if i.get("cls") == "internal"]
    done_internal = sum(
        1
        for e in entries
        if meta.get(e.work_id, {}).get("cls") == "internal"
        and rd.rollup(tasks.get(e.work_id, [])) == rd.ST_DONE
    )
    return {
        "steps": steps,
        "outside": outside,
        "phases": pmeta,
        "drift": drift,
        "dangling": dangling,
        "kpi": {
            "items": len(declared),
            "internal_done": done_internal,
            "internal_total": len(internal),
            "steps_signed": sum(1 for g in gates if g.signed),
            "steps": len(gates),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Sinh dashboard lộ trình hợp nhất.")
    ap.add_argument("--root")
    ap.add_argument("--out")
    a = ap.parse_args()
    root = Path(a.root).resolve() if a.root else rd.find_root()
    out = Path(a.out) if a.out else root / "docs" / "roadmap-dashboard.html"
    tpl = (Path(__file__).resolve().parent / "roadmap_dashboard.tpl.html").read_text(
        encoding="utf-8"
    )
    data = collect(root)
    out.write_text(tpl.replace("__DATA__", json.dumps(data, ensure_ascii=False)), encoding="utf-8")
    k = data["kpi"]
    print(f"dashboard: {out}")
    print(
        f"  {k['steps']} step ({k['steps_signed']} ký) · {k['items']} work item · "
        f"internal {k['internal_done']}/{k['internal_total']} · "
        f"dangling {len(data['dangling'])} · drift {len(data['drift'])}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
