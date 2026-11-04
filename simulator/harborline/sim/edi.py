"""ANSI X12 004010 EDI files in ``landing/edi``.

* ``inbound/940/<CLIENT>``: warehouse shipping orders for EDI-channel orders, batched per client per hour.
* ``outbound/997/<CLIENT>``: functional acknowledgments for every inbound interchange (940 and 856).
* ``inbound/856/<CLIENT>``: advance ship notices for EDI ASNs.
* ``outbound/945/<CLIENT>``: warehouse shipping advices for shipped EDI orders.
* ``archive/<direction>/<kind>/<CLIENT>/<kind>_<CLIENT>_<YYYYMM>.zip``: history older than ARCHIVE_AFTER_DAYS before
  the end of the build is archived per partner per month (members keep their original file names), as translators do.
  Per month because landing is a shared folder on Windows, where every extra file costs far more than its bytes.
  Batches and streams always write loose files.

Envelopes are real: fixed-width ISA (106 characters with the terminator), GS/GE and ISA/IEA control numbers per
trading partner, SE01 = segments in the set, GE01 = sets in the group, IEA01 = groups. Planted: ~0.4% of APEXAUT and
BEACNHW 940 orders never become WMS orders although the 997 accepts them. Defects (malformed envelopes, resends,
late 945s) are logged to the anomaly log.
"""

from __future__ import annotations

import itertools
import re
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ..core import Window, derive_seed
from .crm import as_utc, ascii_slug, module_state, preset_warehouses, sim_clients, to_date
from .files import atomic_write_bytes

HL_QUAL, HL_ID = "ZZ", "HARBORLINE3PL"
ARCHIVE_AFTER_DAYS = 7  # build history older than this many days before history_end goes to monthly zip archives
DROPPED_CLIENTS = {"APEXAUT", "BEACNHW"}
DROP_RATE = 0.004
_BAD = re.compile(r"[*~>^\r\n]")


class Rec(dict):
    """A bus row as a dict with attribute access (pandas 3 itertuples is slow on many small groups)."""

    __getattr__ = dict.get


def records(df: pd.DataFrame, by: list[str]) -> Iterator[tuple[tuple[Any, ...], list[Rec]]]:
    rows = [Rec(r) for r in df.sort_values(by, kind="stable").to_dict("records")]
    for key, grp in itertools.groupby(rows, key=lambda r: tuple(r[k] for k in by[:-1])):
        yield key, list(grp)


def grouped_blocks(keys: np.ndarray, blocks: list[str], qty: np.ndarray, weight: np.ndarray) -> dict[int, tuple[list[str], int, float]]:
    """keys sorted; returns key -> (segments, total qty, total weight) without per-group pandas work."""
    if len(keys) == 0:
        return {}
    starts = np.flatnonzero(np.r_[True, keys[1:] != keys[:-1]])
    ends = np.r_[starts[1:], len(keys)]
    qs = np.add.reduceat(qty, starts)
    ws = np.add.reduceat(weight, starts)
    return {int(keys[a]): ("\x00".join(blocks[a:b]).split("\x00"), int(q), float(w))
            for a, b, q, w in zip(starts, ends, qs, ws, strict=True)}


def el(v: Any, n: int | None = None) -> str:
    """One X12 data element: delimiters stripped, optionally truncated."""
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return ""
    s = _BAD.sub(" ", str(v)).strip()
    return s[:n] if n else s


def isa(sq: str, sid: str, rq: str, rid: str, ts: datetime, ctrl: int, ack: str = "0") -> str:
    seg = (f"ISA*00*{' ' * 10}*00*{' ' * 10}*{sq}*{sid:<15.15}*{rq}*{rid:<15.15}*{ts:%y%m%d}*{ts:%H%M}*U*00401*"
           f"{ctrl:09d}*{ack}*P*>")
    assert len(seg) == 105, seg
    return seg


def partner(seed: int, client: dict[str, Any]) -> dict[str, Any]:
    r = np.random.default_rng(derive_seed(seed, "edi-partner", client["code"]))
    duns = r.random() < 0.3
    pid = f"{int(r.integers(10**8, 10**9))}" if duns else ascii_slug(client["name"]).upper()[:15]
    return {"qual": "01" if duns else "ZZ", "id": pid, "gs": ascii_slug(client["name"]).upper()[:15] or client["code"],
            "newline": bool(r.random() < 0.6), "ctrl0": int(r.integers(1000, 400000))}


def transaction(code: str, ctrl: int, body: list[str]) -> list[str]:
    st = f"ST*{code}*{ctrl:04d}"
    return [st, *body, f"SE*{len(body) + 2}*{ctrl:04d}"]


def interchange(sender: tuple[str, str], receiver: tuple[str, str], gs_ids: tuple[str, str], ts: datetime, fid: str,
                isa_ctrl: int, gs_ctrl: int, sets: list[list[str]]) -> list[str]:
    segs = [isa(sender[0], sender[1], receiver[0], receiver[1], ts, isa_ctrl, "1" if fid in ("OW", "SH") else "0"),
            f"GS*{fid}*{gs_ids[0]}*{gs_ids[1]}*{ts:%Y%m%d}*{ts:%H%M}*{gs_ctrl}*X*004010"]
    for s in sets:
        segs += s
    segs += [f"GE*{len(sets)}*{gs_ctrl}", f"IEA*1*{isa_ctrl:09d}"]
    return segs


def render(segs: list[str], newline: bool) -> bytes:
    term = "~\r\n" if newline else "~"
    return (term.join(segs) + term).encode("ascii", errors="replace")


class Edi:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.st = module_state(ctx, "edi")
        for key in ("ctrl", "wt", "recent", "held", "dropped", "stats", "pending"):
            self.st.setdefault(key, [] if key in ("held", "dropped") else {})
        self.clients = {c["code"]: c for c in sim_clients(ctx)}
        self.whs = preset_warehouses(ctx)
        self.root = Path(ctx.landing_dir) / "edi"
        self.archive_before = (ctx.history_end - timedelta(days=ARCHIVE_AFTER_DAYS)) if ctx.mode == "build" else None
        self.pending: dict[Path, list[tuple[str, bytes, datetime]]] = self.st["pending"]

    # ------------------------------------------------------------------ helpers
    def p(self, code: str) -> dict[str, Any]:
        c = self.clients.get(code) or {"code": code, "name": code}
        return partner(self.ctx.seed, c)

    def next_ctrl(self, key: str, start: int) -> int:
        n = self.st["ctrl"].get(key, start)
        self.st["ctrl"][key] = n + 1
        return n

    def write(self, kind: str, direction: str, code: str, ts: datetime, ctrl: int, data: bytes) -> Path:
        name = f"{kind}_{code}_{ts:%Y%m%d%H%M%S}_{ctrl:09d}.edi"
        self.ctx.changes.add("edi", kind, ins=1)
        if self.archive_before is not None and ts < self.archive_before:
            zpath = self.root / "archive" / direction / kind / code / f"{kind}_{code}_{ts:%Y%m}.zip"
            self.pending.setdefault(zpath, []).append((name, data, ts))
            return Path(f"{zpath}!{name}")
        path = self.root / direction / kind / code / name
        atomic_write_bytes(path, data)
        return path

    def flush_archives(self) -> None:
        for zpath, members in self.pending.items():
            zpath.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(zpath, "a", compression=zipfile.ZIP_DEFLATED) as zf:
                for name, data, ts in members:  # member time = when the partner transmitted it (UTC)
                    info = zipfile.ZipInfo(name, date_time=ts.astimezone(UTC).timetuple()[:6])
                    info.compress_type = zipfile.ZIP_DEFLATED
                    zf.writestr(info, data)
        self.pending.clear()

    def rel(self, path: Path) -> str:
        return str(path.relative_to(Path(self.ctx.landing_dir)))

    def maybe_malform(self, segs: list[str], r: np.random.Generator, label: str, ts: datetime) -> tuple[list[str], str | None]:
        if not self.ctx.dq.hit("malformed_edi", r):
            return segs, None
        kind = str(r.choice(["se_count", "ge_count", "missing_segment"]))
        out = list(segs)
        if kind == "se_count":
            i = next(i for i, s in enumerate(out) if s.startswith("SE*"))
            parts = out[i].split("*")
            parts[1] = str(int(parts[1]) + int(r.choice([-1, 1, 2])))
            out[i] = "*".join(parts)
        elif kind == "ge_count":
            i = next(i for i, s in enumerate(out) if s.startswith("GE*"))
            parts = out[i].split("*")
            parts[1] = str(int(parts[1]) + 1)
            out[i] = "*".join(parts)
        else:
            i = next((i for i, s in enumerate(out) if s.startswith(("G62*", "DTM*", "N4*"))), None)
            if i is not None:
                out.pop(i)
        return out, kind

    def weights(self, codes: list[str]) -> None:
        need = [c for c in codes if c not in self.st["wt"]]
        if not need:
            return
        rows = self.ctx.conn("wms").execute(
            "SELECT o.owner_code, m.sku, CASE WHEN upper(m.weight_uom) = 'KG' THEN m.unit_weight * 2.20462 ELSE m.unit_weight END "
            "FROM wms.material m JOIN wms.owner o ON o.owner_id = m.owner_id WHERE o.owner_code = ANY(%s)", (need,)).fetchall()
        for c in need:
            self.st["wt"][c] = {}
        for code, sku, w in rows:
            self.st["wt"].setdefault(str(code).strip(), {})[sku] = float(w or 0.0)

    def depositor_ref(self, code: str, po: Any) -> str:
        return f"SO-{derive_seed(self.ctx.seed, 'dep', code, str(po)) % 10_000_000:07d}"

    # ------------------------------------------------------------------ 940 + 997
    def inbound_940(self, win: Window) -> None:
        orders = self.ctx.bus.get("orders_created")
        if not len(orders) or "channel" not in orders:
            return
        orders = orders[orders["channel"].astype(str).str.upper() == "EDI"].drop_duplicates("order_id")
        if orders.empty:
            return
        lines = self.ctx.bus.get("order_lines_created")
        lines = lines[lines["order_id"].isin(orders["order_id"])].drop_duplicates(["order_id", "line_nbr"]) if len(lines) else lines
        r = self.ctx.rng("edi", win, "940")
        self.weights(sorted(orders["owner_code"].astype(str).unique()))
        recv = pd.to_datetime(orders["received_ts"], utc=True)
        orders = orders.assign(file_ts=recv - pd.to_timedelta(r.uniform(60, 900, len(orders)), unit="s"))
        orders = orders.assign(hour=orders["file_ts"].dt.floor("h"))
        blocks = self.line_blocks(lines)
        for (code, _hour), rows in records(orders, ["owner_code", "hour", "file_ts"]):
            code = str(code)
            pt = self.p(code)
            ts = as_utc(rows[0]["file_ts"])
            sets: list[list[str]] = []
            for row in rows:
                block, units, wt = blocks.get(int(row.order_id), ([], 0, 0.0))
                if not block:
                    continue
                sets.append(self.order_940(code, row.client_order_ref, row, block, units, wt))
                if code in DROPPED_CLIENTS and r.random() < DROP_RATE:
                    po = self.phantom_po(row.client_order_ref, r)
                    sets.append(self.order_940(code, po, row, block, units, wt))
                    self.st["dropped"].append([code, po, ts.isoformat()])
                    self.st["dropped"] = self.st["dropped"][-5000:]
            if not sets:
                continue
            sets = [transaction("940", i + 1, s) for i, s in enumerate(sets)]
            isa_ctrl = self.next_ctrl(f"isa:{code}", pt["ctrl0"])
            gs_ctrl = self.next_ctrl(f"gs:{code}", pt["ctrl0"] % 9000 + 1)
            segs = interchange((pt["qual"], pt["id"]), (HL_QUAL, HL_ID), (pt["gs"], HL_ID), ts, "OW", isa_ctrl, gs_ctrl, sets)
            segs, bad = self.maybe_malform(segs, r, "940", ts)
            path = self.write("940", "inbound", code, ts, isa_ctrl, render(segs, pt["newline"]))
            if bad:
                self.ctx.dq.log("malformed_edi", "edi", self.rel(path), str(isa_ctrl), bad, None, None, f"940 envelope defect: {bad}", ts)
            if self.ctx.dq.hit("duplicate_row", r):
                again = ts + timedelta(minutes=float(r.uniform(20, 90)))
                dup = self.write("940", "inbound", code, again, isa_ctrl, render(segs, pt["newline"]))
                self.ctx.dq.log("duplicate_row", "edi", self.rel(dup), str(isa_ctrl), None, None, None,
                                f"940 interchange re-sent (same ISA13 as {path.name})", again)
            self.remember(code, path)
            self.ack(code, pt, ts + timedelta(minutes=float(r.uniform(1, 4))), gs_ctrl, "OW", "940", len(sets), bad)

    def line_blocks(self, lines: pd.DataFrame) -> dict[int, tuple[list[str], int, float]]:
        if lines is None or lines.empty:
            return {}
        df = lines.sort_values(["order_id", "line_nbr"])
        qty = pd.to_numeric(df["ordered_qty"], errors="coerce").fillna(0).astype(int)
        uom = df.get("uom", pd.Series("EA", index=df.index)).fillna("EA").astype(str)
        upc = df.get("upc", pd.Series("", index=df.index)).fillna("").astype(str).str.replace(r"[*~>^]", "", regex=True)
        sku = df["sku"].astype(str).str.replace(r"[*~>^]", " ", regex=True)
        desc = df.get("description", pd.Series("", index=df.index)).fillna("").astype(str).str.replace(r"[*~>^\r\n]", " ", regex=True).str[:45]
        n = df.groupby("order_id").cumcount() + 1
        seg_lx = "LX*" + n.astype(str)
        seg_w01 = "W01*" + qty.astype(str) + "*" + uom + "*" + upc + "*VN*" + sku
        seg_g69 = "G69*" + desc
        codes = df["owner_code"].astype(str).tolist() if "owner_code" in df else [""] * len(df)
        wt = np.array([self.st["wt"].get(c, {}).get(k, 0.0) for c, k in zip(codes, df["sku"].astype(str), strict=True)], dtype=float)
        block = (seg_lx + "\x00" + seg_w01 + "\x00" + seg_g69).tolist()
        q = qty.to_numpy()
        return grouped_blocks(df["order_id"].to_numpy(), block, q, wt * q)

    def phantom_po(self, po: Any, r: np.random.Generator) -> str:
        s = str(po or "")
        digits = "".join(str(int(r.integers(0, 10))) if ch.isdigit() else ch for ch in s)
        return digits if digits and digits != s else f"{int(r.integers(10**6, 10**7))}"

    def order_940(self, code: str, po: Any, row: Any, block: list[str], units: int, weight: float) -> list[str]:
        c = self.clients.get(code, {"name": code})
        wh = str(row.warehouse_code)
        whname = self.whs.get(wh, {}).get("name", wh).upper()
        company = getattr(row, "ship_to_company", None)
        name = company if isinstance(company, str) and company else getattr(row, "ship_to_name", "")
        mode = "M" if str(getattr(row, "order_type", "")) == "B2B" and str(row.scac) in ("ODFL", "MRLF", "EXLA", "TRMX") else "U"
        body = [f"W05*N*{self.depositor_ref(code, po)}*{el(po, 22)}",
                f"N1*DE*{el(c['name'], 60).upper()}*ZZ*{code}",
                f"N1*WH*{el(whname, 60)}*ZZ*{wh}",
                f"N1*ST*{el(name, 60).upper()}" + (f"*92*{derive_seed('store', code, name) % 9000 + 100:04d}" if company else ""),
                f"N3*{el(getattr(row, 'ship_to_address1', ''), 55)}",
                f"N4*{el(row.ship_to_city, 30)}*{el(row.ship_to_state, 2)}*{el(row.ship_to_postal, 15)}*{el(row.ship_to_country, 3)}",
                f"N9*PO*{el(po, 30)}"]
        if getattr(row, "requested_ship_date", None) is not None and not pd.isna(row.requested_ship_date):
            body.append(f"G62*10*{to_date(row.requested_ship_date):%Y%m%d}")
        body.append(f"W66*PP*{mode}********{el(row.scac, 4)}")
        body += block
        body.append(f"W76*{units}*{weight:.1f}*LB" if weight > 0 else f"W76*{units}")
        return body

    def ack(self, code: str, pt: dict[str, Any], ts: datetime, gs_ctrl: int, fid: str, txn: str, n: int, bad: str | None) -> None:
        body = [f"AK1*{fid}*{gs_ctrl}"]
        for i in range(1, n + 1):
            body += [f"AK2*{txn}*{i:04d}", "AK5*A" if not bad else "AK5*E*4"]
        body.append(f"AK9*{'A' if not bad else 'E'}*{n}*{n}*{n}")
        isa_ctrl = self.next_ctrl(f"isa_out:{code}", 1)
        gs = self.next_ctrl(f"gs_out:{code}", 1)
        segs = interchange((HL_QUAL, HL_ID), (pt["qual"], pt["id"]), (HL_ID, pt["gs"]), ts, "FA", isa_ctrl, gs,
                           [transaction("997", 1, body)])
        self.write("997", "outbound", code, ts, isa_ctrl, render(segs, True))

    def remember(self, code: str, path: Path) -> None:
        lst = self.st["recent"].setdefault(code, [])
        lst.append(self.rel(path))
        self.st["recent"][code] = lst[-20:]

    # ------------------------------------------------------------------ 856 (inbound ASN)
    def inbound_856(self, win: Window) -> None:
        asns = self.ctx.bus.get("asns_created")
        if not len(asns) or "source" not in asns:
            return
        asns = asns[asns["source"].astype(str).str.upper().isin(["EDI856", "EDI"])].drop_duplicates("asn_nbr")
        if asns.empty:
            return
        r = self.ctx.rng("edi", win, "856")
        span = max(1.0, (win.end - win.start).total_seconds() - 1)
        asns = asns.assign(file_ts=[win.start + timedelta(seconds=float(x)) for x in r.uniform(0, span, len(asns))])
        asns = asns.assign(hour=pd.to_datetime(asns["file_ts"], utc=True).dt.floor("h"))
        for (code, _hour), rows in records(asns, ["owner_code", "hour", "file_ts"]):
            code = str(code)
            pt = self.p(code)
            ts = as_utc(rows[0]["file_ts"])
            sets = [transaction("856", i + 1, self.asn_body(code, row, r, ts)) for i, row in enumerate(rows)]
            isa_ctrl = self.next_ctrl(f"isa:{code}", pt["ctrl0"])
            gs_ctrl = self.next_ctrl(f"gs:{code}", pt["ctrl0"] % 9000 + 1)
            segs = interchange((pt["qual"], pt["id"]), (HL_QUAL, HL_ID), (pt["gs"], HL_ID), ts, "SH", isa_ctrl, gs_ctrl, sets)
            segs, bad = self.maybe_malform(segs, r, "856", ts)
            path = self.write("856", "inbound", code, ts, isa_ctrl, render(segs, pt["newline"]))
            if bad:
                self.ctx.dq.log("malformed_edi", "edi", self.rel(path), str(isa_ctrl), bad, None, None, f"856 envelope defect: {bad}", ts)
            self.ack(code, pt, ts + timedelta(minutes=float(r.uniform(1, 4))), gs_ctrl, "SH", "856", len(sets), bad)

    def asn_body(self, code: str, row: Any, r: np.random.Generator, ts: datetime) -> list[str]:
        wh = str(row.warehouse_code)
        exp = to_date(row.expected_date)
        lines = list(row.lines) if isinstance(row.lines, (list, tuple, np.ndarray)) else []
        qty = sum(int(x.get("qty", 0) or 0) for x in lines)
        pallets = max(1, round(qty / 400))
        scac = str(r.choice(["ODFL", "MRLF", "EXLA"])) if wh not in ("TOR1", "MTY1") else ("ODFL" if wh == "TOR1" else "TRMX")
        body = [f"BSN*00*{el(row.asn_nbr, 30)}*{ts:%Y%m%d}*{ts:%H%M}*0001",
                f"DTM*011*{exp - timedelta(days=int(r.integers(1, 5))):%Y%m%d}", f"DTM*017*{exp:%Y%m%d}",
                "HL*1**S", f"TD1*PLT*{pallets}****G*{pallets * 900}*LB", f"TD5**2*{scac}",
                f"REF*BM*{int(r.integers(10**8, 10**9))}", f"N1*SF*{el(row.supplier_name, 60).upper()}",
                f"N1*ST*{el(self.whs.get(wh, {}).get('name', wh), 60).upper()}*ZZ*{wh}",
                "HL*2*1*O", f"PRF*{el(row.asn_nbr, 22).replace('ASN-', 'PO')}"]
        hl = 2
        for x in lines:
            hl += 1
            sku, upc, lot = el(x.get("sku"), 48), el(x.get("upc")), el(x.get("lot"), 30)
            lin = f"LIN**VN*{sku}" + (f"*UP*{upc}" if upc else "") + (f"*LT*{lot}" if lot else "")
            body += [f"HL*{hl}*2*I", lin, f"SN1**{int(x.get('qty', 0) or 0)}*{el(x.get('uom') or 'EA', 2)}"]
            exp = x.get("expiry")
            if exp is not None and not pd.isna(exp):
                body.append(f"DTM*036*{to_date(exp):%Y%m%d}")
        body.append(f"CTT*{hl}")
        return body

    # ------------------------------------------------------------------ 945 (outbound shipping advice)
    def outbound_945(self, win: Window) -> None:
        ships = self.ctx.bus.get("shipments_shipped")
        if not len(ships) or "channel" not in ships:
            return
        ships = ships[ships["channel"].astype(str).str.upper() == "EDI"].drop_duplicates("shipment_id")
        if ships.empty:
            return
        r = self.ctx.rng("edi", win, "945")
        lines = self.ctx.bus.get("shipment_lines")
        lines = lines[lines["shipment_id"].isin(ships["shipment_id"])].drop_duplicates(["shipment_id", "line_nbr"]) if len(lines) else lines
        hdr = self.ship_to(ships["order_id"].astype("int64").tolist())
        sts = pd.to_datetime(ships["ship_ts"], utc=True)
        ships = ships.assign(file_ts=sts + pd.to_timedelta(r.uniform(300, 2400, len(ships)), unit="s"))
        ships = ships.assign(hour=ships["file_ts"].dt.floor("h"))
        by_ship = self.ship_blocks(lines)
        for (code, _hour), rows in records(ships, ["owner_code", "hour", "file_ts"]):
            code = str(code)
            pt = self.p(code)
            ts = as_utc(rows[0]["file_ts"])
            sets = []
            for row in rows:
                blk = by_ship.get(int(row.shipment_id))
                if blk is None:
                    continue
                sets.append(self.ship_945(code, row, blk, hdr.get(int(row.order_id), {})))
            if not sets:
                continue
            sets = [transaction("945", i + 1, s) for i, s in enumerate(sets)]
            isa_ctrl = self.next_ctrl(f"isa_out:{code}", 1)
            gs_ctrl = self.next_ctrl(f"gs_out:{code}", 1)
            segs = interchange((HL_QUAL, HL_ID), (pt["qual"], pt["id"]), (HL_ID, pt["gs"]), ts, "SW", isa_ctrl, gs_ctrl, sets)
            segs, bad = self.maybe_malform(segs, r, "945", ts)
            data = render(segs, True)
            if self.ctx.dq.hit("late_arriving", r):
                late = ts + timedelta(hours=float(r.uniform(20, 72)))
                self.st["held"].append([late.isoformat(), code, isa_ctrl, data.decode("ascii")])
                self.ctx.dq.log("late_arriving", "edi", f"edi/outbound/945/{code}", str(isa_ctrl), None, ts.isoformat(), late.isoformat(),
                                "945 transmitted late (translator queue stuck)", ts)
                continue
            path = self.write("945", "outbound", code, ts, isa_ctrl, data)
            if bad:
                self.ctx.dq.log("malformed_edi", "edi", self.rel(path), str(isa_ctrl), bad, None, None, f"945 envelope defect: {bad}", ts)

    def ship_to(self, order_ids: list[int]) -> dict[int, dict[str, Any]]:
        if not order_ids:
            return {}
        rows = self.ctx.conn("wms").execute(
            "SELECT order_id, ship_to_name, ship_to_company, ship_to_address1, ship_to_city, ship_to_state, ship_to_postal, "
            "ship_to_country FROM wms.order_header WHERE order_id = ANY(%s)", (order_ids,)).fetchall()
        return {int(r[0]): {"name": r[1], "company": r[2], "addr": r[3], "city": r[4], "state": r[5], "postal": r[6], "country": r[7]}
                for r in rows}

    @staticmethod
    def ship_blocks(lines: pd.DataFrame) -> dict[int, tuple[list[str], int, float]]:
        if lines is None or lines.empty:
            return {}
        df = lines.sort_values(["shipment_id", "line_nbr"])
        o = pd.to_numeric(df["ordered_qty"], errors="coerce").fillna(0).astype(int)
        q = pd.to_numeric(df["shipped_qty"], errors="coerce").fillna(0).astype(int)
        status = pd.Series(np.where(q >= o, "CC", np.where(q > 0, "PR", "BP")), index=df.index)
        n = df.groupby("shipment_id").cumcount() + 1
        upc = df.get("upc", pd.Series("", index=df.index)).fillna("").astype(str).str.replace(r"[*~>^]", "", regex=True)
        sku = df["sku"].astype(str).str.replace(r"[*~>^]", " ", regex=True)
        lot = df.get("lot_nbr", pd.Series("", index=df.index)).fillna("").astype(str).str.replace(r"[*~>^]", " ", regex=True)
        w12 = ("W12*" + status + "*" + o.astype(str) + "*" + q.astype(str) + "*" + (o - q).clip(lower=0).astype(str) + "*EA*" + upc
               + "*VN*" + sku + lot.where(lot == "", "*" + lot))
        block = ("LX*" + n.astype(str) + "\x00" + w12).tolist()
        return grouped_blocks(df["shipment_id"].to_numpy(), block, q.to_numpy(), np.zeros(len(df)))

    def ship_945(self, code: str, row: Any, blk: tuple[list[str], int, float], to: dict[str, Any]) -> list[str]:
        wh = str(row.warehouse_code)
        ship_d = to_date(row.ship_ts_local if row.ship_ts_local is not None else row.ship_ts)
        po = row.client_order_ref
        name = to.get("company") or to.get("name") or ""
        mode = "M" if str(row.mode or "").upper() == "LTL" else "U"
        body = [f"W06*F*{self.depositor_ref(code, po)}*{ship_d:%Y%m%d}*{el(row.shipment_nbr, 30)}*{el(row.order_nbr, 30)}*{el(po, 22)}",
                f"N1*ST*{el(name, 60).upper()}", f"N3*{el(to.get('addr'), 55)}",
                (f"N4*{el(to.get('city') or row.dest_city, 30)}*{el(to.get('state') or row.dest_state, 2)}*"
                 f"{el(to.get('postal') or row.dest_postal, 15)}*{el(to.get('country') or row.dest_country, 3)}"),
                f"N1*SF*{el(self.whs.get(wh, {}).get('name', wh), 60).upper()}*ZZ*{wh}"]
        if isinstance(row.bol_nbr, str) and row.bol_nbr:
            body.append(f"N9*BM*{el(row.bol_nbr, 30)}")
        if isinstance(row.tracking_nbr, str) and row.tracking_nbr:
            body.append(f"N9*CN*{el(row.tracking_nbr, 30)}")
        body += [f"G62*11*{ship_d:%Y%m%d}", f"W27*{mode}*{el(row.scac, 4)}"]
        segs, units, _ = blk
        body += segs
        wt = float(row.total_weight) if row.total_weight is not None and not pd.isna(row.total_weight) else 0.0
        uom = str(row.weight_uom).upper() if isinstance(row.weight_uom, str) else "LB"
        body.append(f"W03*{units}*{wt:.1f}*{uom}")
        return body

    def release_held(self, win: Window) -> None:
        keep = []
        for item in self.st["held"]:
            when = datetime.fromisoformat(item[0])
            if when < win.end:
                self.write("945", "outbound", item[1], max(when, win.start), int(item[2]), item[3].encode("ascii"))
            else:
                keep.append(item)
        self.st["held"] = keep

    # ------------------------------------------------------------------ run
    def step(self, win: Window) -> None:
        if win.crosses_local_time("UTC", 5, 0) and win.start.weekday() == 0:
            self.st["wt"] = {}
        self.release_held(win)
        self.inbound_940(win)
        self.inbound_856(win)
        self.outbound_945(win)
        if win.end.day == 1 or (self.archive_before is not None and win.end >= self.archive_before):
            self.flush_archives()

    def resend(self, ts: datetime, code: str | None, skip: int = 0) -> str | None:
        """duplicate_file_resend: a trading partner re-sends a recent 940 interchange (same ISA control number)."""
        recent = self.st["recent"]
        candidates = [code] if code in recent else sorted(recent, key=lambda k: recent[k][-1] if recent[k] else "", reverse=True)
        for c in candidates[skip:] + candidates[:skip]:
            if not recent.get(c):
                continue
            src = Path(self.ctx.landing_dir) / recent[c][-1]
            data = read_document(src)
            if data is None:
                continue
            src_name = src.name.rsplit("!", 1)[-1]
            ctrl = int(src_name.removesuffix(".edi").rsplit("_", 1)[1])
            dst = self.write("940", "inbound", c, ts, ctrl, data)
            self.ctx.dq.log("duplicate_row", "edi", self.rel(dst), str(ctrl), None, None, None,
                            f"scenario duplicate_file_resend: same interchange as {src_name}", ts)
            return f"EDI: {c} re-sent {src_name} as {dst.name} (ISA13 {ctrl:09d})"
        return None


def read_document(path: Path) -> bytes | None:
    """Bytes of a loose file, or of an archive member addressed as ``<zip path>!<member>``; None if missing."""
    zpath, sep, member = str(path).rpartition("!")
    if not sep:
        return path.read_bytes() if path.exists() else None
    if not Path(zpath).exists():
        return None
    with zipfile.ZipFile(zpath) as zf:
        return zf.read(member) if member in zf.namelist() else None


def iter_documents(edi_root: Path, sub: str = "") -> Iterator[tuple[str, bytes]]:
    """(file name, bytes) for every EDI document under ``edi_root/sub`` (e.g. sub="inbound/940"): loose ``.edi``
    files plus the members of the matching monthly archives under ``edi_root/archive/sub``."""
    live = edi_root / sub if sub else edi_root
    for p in sorted(live.rglob("*.edi")):
        if "archive" not in p.relative_to(edi_root).parts:
            yield p.name, p.read_bytes()
    for z in sorted((edi_root / "archive" / sub).rglob("*.zip")):
        with zipfile.ZipFile(z) as zf:
            for name in sorted(zf.namelist()):
                yield name, zf.read(name)


class EdiModule:
    name = "edi"

    def bootstrap(self, ctx: Any) -> None:
        Edi(ctx)
        (Path(ctx.landing_dir) / "edi").mkdir(parents=True, exist_ok=True)

    def step(self, ctx: Any, win: Window) -> None:
        Edi(ctx).step(win)

    def apply_scenario(self, ctx: Any, scenario_id: str, params: dict[str, Any], win: Window) -> str | None:
        if scenario_id == "duplicate_file_resend" and params.get("kind", "all") in ("all", "edi"):
            edi = Edi(ctx)
            notes = [n for n in (edi.resend(win.start + timedelta(minutes=i), None, i) for i in range(int(params.get("count") or 1))) if n]
            edi.flush_archives()
            return "; ".join(notes) or None
        return None


MODULE = EdiModule()
