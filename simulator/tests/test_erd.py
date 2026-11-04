"""ER diagrams (handbook/source-systems.html) and plain-text ERDs (handbook/erd/*.mmd) match the schemas and the code.

No database needed: everything comes from the DDL, the Salesforce YAML and the simulator modules.
"""

from __future__ import annotations

import itertools
import re
from datetime import UTC, datetime

import pytest
import yaml

from harborline.tools import build_data_dictionary as b

ROOT = b.DEFAULT_ROOT
PAGE = ROOT / "handbook" / "source-systems.html"
ERD = ROOT / "handbook" / "erd"
# "<source>.<entity>" -> why it is in no diagram. Keep it empty unless there is a real reason.
EXEMPT: dict[str, str] = {}


@pytest.fixture(scope="module")
def cat() -> b.Catalog:
    return b.load_catalog()


@pytest.fixture(scope="module")
def diagrams() -> list[b.Diagram]:
    return b.diagrams()


def test_catalog_has_every_table_and_object(cat: b.Catalog) -> None:
    ddl = {
        "wms": b.SCHEMA_DIR / "wms.sql",
        "ax2012": b.SCHEMA_DIR / "ax2012.sql",
        "iot": b.SCHEMA_DIR / "iot.sql",
        "tracker": b.TRACKER_SQL,
    }
    for source, path in ddl.items():
        text = path.read_text(encoding="utf-8")
        tables = re.findall(r"^CREATE TABLE (?:IF NOT EXISTS )?(?:\w+\.)?(\w+)", text, re.MULTILINE)
        assert sorted(tables) == sorted(cat[source]), source
        for name, ent in cat[source].items():
            body = re.search(rf"CREATE TABLE (?:IF NOT EXISTS )?(?:\w+\.)?{name} \((.*?)\n\);", text, re.DOTALL)
            assert body and len(ent.fields) >= 2, f"{source}.{name} parsed badly"
    spec = yaml.safe_load((b.SCHEMA_DIR / "salesforce_objects.yaml").read_text(encoding="utf-8"))
    assert sorted(spec["objects"]) == sorted(cat["salesforce"])


def test_every_table_object_and_file_is_in_a_diagram(cat: b.Catalog, diagrams: list[b.Diagram]) -> None:
    drawn = {box.entity for d in diagrams for box in d.boxes}
    every = {f"{s}.{n}" for s, ents in cat.items() for n in ents}
    missing = sorted(every - drawn - set(EXEMPT))
    assert not missing, f"in no diagram (draw them, or add them to EXEMPT with a reason): {missing}"
    assert not set(EXEMPT) & drawn, "exempt entities are drawn after all"
    assert all(EXEMPT.values()), "every exemption needs a written reason"
    assert drawn <= every, f"boxes for unknown entities: {sorted(drawn - every)}"


def test_boxes_and_lines_match_the_schemas(cat: b.Catalog, diagrams: list[b.Diagram]) -> None:
    assert len({d.name for d in diagrams}) == len(diagrams)
    for d in diagrams:
        assert d.source in b.SOURCES and d.name.startswith("er-")
        boxes: dict[str, b.Box] = {}
        for box in d.boxes:
            assert box.id not in boxes, f"{d.name}: two boxes called {box.id}"
            boxes[box.id] = box
            ent = cat[box.source].get(box.name)
            assert ent is not None, f"{d.name}: no entity {box.entity}"
            shown = [r.field for r in box.parsed]
            assert len(shown) == len(set(shown)), f"{d.name}: {box.id} shows a column twice"
            for field in shown:
                assert ent.get(field), f"{d.name}: {box.entity} has no column {field!r}"
        for e in d.edges:
            cb, pb = boxes[e.child[0]], boxes[e.parent[0]]
            where = f"{d.name}: {cb.entity}.{e.child[1]} -> {pb.entity}.{e.parent[1]}"
            for box, field in ((cb, e.child[1]), (pb, e.parent[1])):
                assert not field or field in [r.field for r in box.parsed], f"{where}: the box does not show {field}"
            cf = cat[cb.source][cb.name].get(e.child[1]) if e.child[1] else None
            pf = cat[pb.source][pb.name].get(e.parent[1]) if e.parent[1] else None
            assert e.kind in b.KINDS, where
            if e.kind == "fk":
                assert cf and pf and cb.source == pb.source, where
                assert pb.name in cf.refs, f"{where}: drawn solid but the schema declares no such reference"
                assert pf.pk, f"{where}: a foreign key points at the primary key"
            elif e.kind == "app":
                assert cf and pf and cb.source == pb.source, where
                assert pb.name not in cf.refs, f"{where}: a declared foreign key is drawn dashed"
            elif e.kind == "x":
                assert cf and pf and cb.source != pb.source, f"{where}: teal lines join two systems"
            else:
                assert cb.source == pb.source and cb.source in b.VIRTUAL, f"{where}: nesting is for files and payloads"


def test_files_payloads_and_segments_match_the_code(cat: b.Catalog) -> None:
    for source in b.VIRTUAL:
        for ent in cat[source].values():
            code = (b.PACKAGE_DIR / ent.module).read_text(encoding="utf-8")
            names = [f.name for f in ent.fields]
            assert len(names) == len(set(names)), f"{source}.{ent.name}: duplicate field names"
            for f in ent.fields:
                assert f.evidence and f.evidence in code, (
                    f"{source}.{ent.name}.{f.name}: {f.evidence!r} not in {ent.module}"
                )


def test_tracking_event_fields_are_the_api_payload(cat: b.Catalog) -> None:
    from harborline.api import carrier_api

    row = (
        1,
        "1Z0000000000000000",
        "UPSN",
        "DELIVERED",
        "2026-01-05T14:03:22-05:00",
        "Delivered",
        None,
        "Atlanta",
        "GA",
        "US",
        datetime(2026, 1, 5, 19, 10, tzinfo=UTC),
        False,
    )
    event = carrier_api._event(row)
    flat = [k for k in event if k != "location"] + [f"location_{k}" for k in event["location"]]
    assert sorted(flat) == sorted(f.name for f in cat["carrier_api"]["tracking_event"].fields)


def test_generated_blocks_and_mermaid_files_are_up_to_date() -> None:
    blocks, mmd = b.outputs()
    html = PAGE.read_text(encoding="utf-8")
    assert set(re.findall(r"<!-- BEGIN GENERATED: ([\w-]+) -->", html)) == set(blocks)
    stale = []
    for name, content in blocks.items():
        m = re.search(
            rf"<!-- BEGIN GENERATED: {re.escape(name)} -->\n(.*?)\n<!-- END GENERATED: {re.escape(name)} -->",
            html,
            re.DOTALL,
        )
        if not m or m.group(1) != content:
            stale.append(name)
    for source, text in mmd.items():
        path = ERD / f"{source}.mmd"
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            stale.append(path.name)
    assert not stale, f"stale, run python -m harborline.tools.build_data_dictionary: {stale}"
    assert sorted(p.name for p in ERD.glob("*.mmd")) == sorted(f"{s}.mmd" for s in mmd)


@pytest.mark.parametrize("name", [d.name for d in b.diagrams()])
def test_diagram_layout_has_no_overlaps_or_clipped_text(name: str, cat: b.Catalog) -> None:
    ds = b.diagrams()
    d = next(x for x in ds if x.name == name)
    assert b.layout_problems(d, cat, b.fk_children(ds)) == []


def test_boxes_on_the_page_do_not_overlap() -> None:
    """Independent of the generator: read every diagram SVG on the page and compare its box rectangles."""
    html = PAGE.read_text(encoding="utf-8")
    svgs = re.findall(r"<!-- BEGIN GENERATED: (er-[\w-]+) -->\n(<svg.*?</svg>)", html, re.DOTALL)
    assert len(svgs) == len(b.diagrams()) + 1  # plus the cross-system keys
    for name, svg in svgs:
        w, h = map(float, re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg).groups())
        rects = [
            tuple(map(float, r))
            for r in re.findall(
                r'<rect class="dg-box[^"]*" x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" height="([\d.]+)"', svg
            )
        ]
        assert rects, name
        for x, y, rw, rh in rects:
            assert x >= 0 and y >= 0 and x + rw <= w and y + rh <= h, f"{name}: box outside the picture"
        for (ax, ay, aw, ah), (bx, by, bw, bh) in itertools.combinations(rects, 2):
            assert ax + aw <= bx or bx + bw <= ax or ay + ah <= by or by + bh <= ay, f"{name}: boxes overlap"


MMD_LINES = [
    re.compile(r"erDiagram"),
    re.compile(r"    %% \S.*"),
    re.compile(r"    [A-Za-z_][\w-]* \{"),
    re.compile(r'        [A-Za-z_][\w\-()\[\]]* [A-Za-z_][\w-]*( (PK|FK|UK)(, (PK|FK|UK))*)?( "[^"]*")?'),
    re.compile(r"    \}"),
    re.compile(r'    [A-Za-z_][\w-]* (\|o|\|\|)(--|\.\.)(o\{|o\||\|\{|\|\|) [A-Za-z_][\w-]* : "[^"]+"'),
    re.compile(r""),
]


def test_mermaid_files_are_valid_and_complete(cat: b.Catalog) -> None:
    _, mmd = b.outputs()
    for source, text in mmd.items():
        lines = text.split("\n")
        assert lines[0] == "erDiagram"
        bad = [ln for ln in lines if not any(p.fullmatch(ln) for p in MMD_LINES)]
        assert not bad, f"{source}.mmd: lines Mermaid will not parse: {bad[:3]}"
        defined = re.findall(r"^    ([A-Za-z_][\w-]*) \{$", text, re.MULTILINE)
        assert len(defined) == len(set(defined)), f"{source}.mmd: an entity is defined twice"
        for rel in re.findall(r"^    (\S+) \S+ (\S+) : ", text, re.MULTILINE):
            assert set(rel) <= set(defined), f"{source}.mmd: relationship with an undefined entity {rel}"
        for name, ent in cat[source].items():
            block = re.search(rf"^    {re.escape(name)} \{{\n(.*?)\n    \}}", text, re.MULTILINE | re.DOTALL)
            assert block, f"{source}.mmd: no entity {name}"
            got = [ln.split()[1] for ln in block.group(1).split("\n")]
            assert got == [f.name for f in ent.fields], f"{source}.mmd: {name} does not list every column in order"
