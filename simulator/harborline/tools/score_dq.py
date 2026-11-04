"""Score your data-quality findings against the defects the simulator injected.

Your findings file is a CSV with a header row and the columns ``system, object, record_key`` and optionally
``category`` (aliases: ``table`` for object, ``key`` for record_key). ``system`` is one of wms, ax2012, iot,
salesforce, carrier, edi, files; ``object`` is the table, Salesforce object or file name.

Run it inside the app container (put the CSV in the landing folder so the container can see it)::

    docker compose exec app python -m harborline.tools.score_dq /data/landing/my_findings.csv
    docker compose exec app python -m harborline.tools.score_dq /data/landing/my_findings.csv --until 2026-10-15

Without a database it can read the mirror file instead: ``--anomalies /data/answer_key/anomalies.csv``.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

Key = tuple[str, str, str]

ALIASES = {"table": "object", "key": "record_key", "record": "record_key", "type": "category"}


@dataclass(frozen=True)
class Anomaly:
    key: Key
    category: str
    sim_ts: str


def normalize(system: str | None, obj: str | None, record_key: str | None) -> Key:
    o = (obj or "").strip().replace("\\", "/").lower()
    if "/" in o:
        o = o.rsplit("/", 1)[1]
    for prefix in ("wms.", "dbo.", "telemetry.", "public."):
        o = o.removeprefix(prefix)
    return ((system or "").strip().lower(), o, (record_key or "").strip().lower())


def read_findings(path: Path) -> list[tuple[Key, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise SystemExit(f"{path}: empty file")
        names = {f: ALIASES.get(f.strip().lower(), f.strip().lower()) for f in reader.fieldnames}
        missing = {"system", "object", "record_key"} - set(names.values())
        if missing:
            raise SystemExit(f"{path}: missing column(s) {sorted(missing)}; found {reader.fieldnames}")
        out, seen = [], set()
        for raw in reader:
            row = {names[k]: v for k, v in raw.items() if k in names}
            key = normalize(row.get("system"), row.get("object"), row.get("record_key"))
            if key not in seen:
                seen.add(key)
                out.append((key, (row.get("category") or "").strip().lower()))
        return out


def anomalies_from_csv(path: Path) -> list[Anomaly]:
    with path.open(encoding="utf-8", newline="") as fh:
        return [Anomaly(normalize(r["system"], r["object"], r["record_key"]), r["category"], r.get("sim_ts") or "")
                for r in csv.DictReader(fh)]


def anomalies_from_db() -> list[Anomaly]:
    from ..db import get_db

    rows = get_db().fetchall("sim_internal",
                             "SELECT system, object, record_key, category, sim_ts FROM sim.anomaly_log ORDER BY anomaly_id")
    return [Anomaly(normalize(s, o, k), c, ts.isoformat() if ts else "") for s, o, k, c, ts in rows]


def score(findings: list[tuple[Key, str]], anomalies: list[Anomaly]) -> dict:
    by_key: dict[Key, set[str]] = defaultdict(set)
    for a in anomalies:
        by_key[a.key].add(a.category)
    logged = Counter(a.category for a in anomalies)
    found_keys = {k for k, _ in findings if k in by_key}
    found = Counter(a.category for a in anomalies if a.key in found_keys)
    labelled = Counter(c for _, c in findings if c)
    labelled_ok = Counter(c for k, c in findings if c and c in by_key.get(k, ()))
    matched = sum(1 for k, _ in findings if k in by_key)
    return {
        "findings": len(findings), "anomalies": len(anomalies), "matched_findings": matched,
        "precision": matched / len(findings) if findings else 0.0,
        "recall": sum(found.values()) / len(anomalies) if anomalies else 0.0,
        "categories": {
            c: {"logged": logged[c], "found": found[c], "recall": found[c] / logged[c] if logged[c] else 0.0,
                "labelled": labelled[c], "precision": labelled_ok[c] / labelled[c] if labelled[c] else None}
            for c in sorted(set(logged) | set(labelled))
        },
        "missed": [a for a in anomalies if a.key not in found_keys],
        "false_positives": [k for k, _ in findings if k not in by_key],
    }


def _pct(x: float | None) -> str:
    return "    -" if x is None else f"{100 * x:5.1f}%"


def report(result: dict, show: int) -> str:
    lines = [
        f"Findings: {result['findings']:,}   Injected defects: {result['anomalies']:,}",
        "",
        f"{'category':<22}{'injected':>9}{'found':>8}{'recall':>9}{'you tagged':>12}{'precision':>11}",
    ]
    for cat, v in result["categories"].items():
        lines.append(f"{cat:<22}{v['logged']:>9,}{v['found']:>8,}{_pct(v['recall']):>9}{v['labelled']:>12,}{_pct(v['precision']):>11}")
    lines += [
        "",
        (f"Overall precision: {_pct(result['precision']).strip()}  ({result['matched_findings']:,} of {result['findings']:,} findings "
        "match an injected defect)"),
        f"Overall recall:    {_pct(result['recall']).strip()}",
    ]
    if show:
        lines += ["", f"Examples of missed defects (first {show}):"]
        lines += [f"  {a.category:<20} {a.key[0]:<10} {a.key[1]:<28} {a.key[2]}" for a in result["missed"][:show]]
        lines += ["", f"Examples of findings that are not injected defects (first {show}):"]
        lines += [f"  {k[0]:<10} {k[1]:<28} {k[2]}" for k in result["false_positives"][:show]]
    lines.append("\nNote: precision counts only injected defects. Systematic quirks (time zones, units, blank crosswalk "
                 "codes) are real-world traps, not injected defects.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("findings", type=Path, help="your findings CSV")
    parser.add_argument("--anomalies", type=Path, help="read defects from this anomalies.csv instead of the database")
    parser.add_argument("--until", help="only count defects injected at or before this simulated time (ISO date/time)")
    parser.add_argument("--system", help="only score one system (wms, ax2012, iot, salesforce, carrier, edi, files)")
    parser.add_argument("--show", type=int, default=10, help="number of missed / false-positive examples to print")
    args = parser.parse_args(argv)

    anomalies = anomalies_from_csv(args.anomalies) if args.anomalies else anomalies_from_db()
    findings = read_findings(args.findings)
    if args.until:
        anomalies = [a for a in anomalies if not a.sim_ts or a.sim_ts[: len(args.until)] <= args.until]
    if args.system:
        system = args.system.strip().lower()
        anomalies = [a for a in anomalies if a.key[0] == system]
        findings = [f for f in findings if f[0][0] == system]
    print(report(score(findings, anomalies), args.show))
    return 0


if __name__ == "__main__":
    sys.exit(main())
