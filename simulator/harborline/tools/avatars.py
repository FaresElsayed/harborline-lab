"""Illustrated portraits of the fictional Harborline colleagues, as small self-contained SVG files.

Nobody here is a real person. build_team_page.py writes one file per colleague to handbook/assets/people/.
"""

from __future__ import annotations

SKIN = {
    "fair": ("#F6D9C4", "#E3B79B"),
    "light": ("#EFC9A9", "#D9A984"),
    "tan": ("#DDA77C", "#C0875C"),
    "olive": ("#CC9A6E", "#AE7B50"),
    "brown": ("#A9744C", "#8A5A37"),
    "deep": ("#7B4F32", "#603B23"),
}

BACKGROUND = {
    "EXEC": "#C9D5E3",
    "FIN": "#E8DEC6",
    "OPS": "#C4E2DE",
    "SLS": "#F2CFD8",
    "CS": "#E4D0EA",
    "QA": "#D3E5C4",
    "IT": "#CDD4F0",
}

# skin, hair style, hair colour, top colour, and optional glasses / beard / mustache / scarf.
LOOKS: dict[str, dict[str, str]] = {
    "daniel.reyes": {
        "skin": "tan",
        "hair": "side",
        "color": "#2B2420",
        "top": "#1E3550",
        "wear": "blazer",
        "temples": "#8E8A86",
    },
    "rebecca.lindqvist": {
        "skin": "fair",
        "hair": "bob",
        "color": "#D8B46A",
        "top": "#3A4250",
        "wear": "blazer",
        "glasses": "#2C2C2C",
    },
    "alan.brooks": {
        "skin": "light",
        "hair": "balding",
        "color": "#A8A29A",
        "top": "#6B7A5E",
        "wear": "shirt",
        "glasses": "#5A4A3A",
        "mustache": "#A8A29A",
    },
    "priya.raman": {"skin": "brown", "hair": "long", "color": "#17120F", "top": "#0F7C78", "wear": "round"},
    "luis.moreno": {
        "skin": "tan",
        "hair": "short",
        "color": "#1C1815",
        "top": "#D9692A",
        "wear": "polo",
        "beard": "#2A2420",
    },
    "shanice.carter": {"skin": "deep", "hair": "afro", "color": "#17120F", "top": "#E2A52E", "wear": "round"},
    "beth.kowalczyk": {"skin": "fair", "hair": "curly", "color": "#9C4A25", "top": "#7E3B3B", "wear": "shirt"},
    "hannah.price": {"skin": "light", "hair": "ponytail", "color": "#6A4326", "top": "#D65F5F", "wear": "blazer"},
    "omar.haddad": {
        "skin": "olive",
        "hair": "short",
        "color": "#1E1A17",
        "top": "#6A4A9C",
        "wear": "shirt",
        "beard": "#26201C",
    },
    "ayesha.khan": {
        "skin": "brown",
        "hair": "scarf",
        "color": "#2F6F5B",
        "top": "#F2F4F2",
        "wear": "coat",
        "glasses": "#3A3A3A",
    },
    "mei.tanaka": {"skin": "light", "hair": "fringe", "color": "#141210", "top": "#26303C", "wear": "round"},
    "sam.okafor": {"skin": "deep", "hair": "buzz", "color": "#17120F", "top": "#2F6DB5", "wear": "shirt"},
    "nadia.petrov": {"skin": "fair", "hair": "ponytail", "color": "#3B2A20", "top": "#2C3E66", "wear": "shirt"},
    "marcus.bell": {
        "skin": "brown",
        "hair": "bald",
        "color": "#17120F",
        "top": "#23272C",
        "wear": "round",
        "beard": "#1E1815",
        "glasses": "#111111",
    },
    "dana.whitfield": {"skin": "fair", "hair": "wavy", "color": "#B9702F", "top": "#1F8A8A", "wear": "round"},
    "tomas.ortega": {
        "skin": "tan",
        "hair": "wavy_short",
        "color": "#2A201A",
        "top": "#4F7A45",
        "wear": "shirt",
        "mustache": "#2A201A",
    },
    "grace.liu": {
        "skin": "light",
        "hair": "bun",
        "color": "#191512",
        "top": "#C99A2E",
        "wear": "round",
        "glasses": "#7A2E2E",
    },
    "kevin.ohara": {
        "skin": "fair",
        "hair": "short",
        "color": "#B4602B",
        "top": "#4C5866",
        "wear": "polo",
        "beard": "#A9582A",
    },
    "iris.novak": {
        "skin": "light",
        "hair": "bob_streak",
        "color": "#2C2522",
        "top": "#7A2E3F",
        "wear": "blazer",
        "glasses": "#1E1E1E",
    },
}


def _back_hair(style: str, c: str) -> str:
    if style == "long":
        return f'<path fill="{c}" d="M37 52c0-22 12-32 27-32s27 10 27 32v50c0 5-8 8-13 8H50c-5 0-13-3-13-8z"/>'
    if style == "wavy":
        return (
            f'<path fill="{c}" d="M36 50c0-21 13-31 28-31s28 10 28 31c0 10 4 15 4 24s-5 12-4 20-5 15-13 15H49'
            f'c-8 0-14-7-13-15s-4-11-4-20 4-14 4-24z"/>'
        )
    if style in {"bob", "bob_streak"}:
        return f'<path fill="{c}" d="M36 52c0-22 13-32 28-32s28 10 28 32v26c0 6-5 9-11 9H47c-6 0-11-3-11-9z"/>'
    if style == "ponytail":
        return f'<path fill="{c}" d="M84 36c13 3 18 16 15 32-2 11-6 19-13 24 2-14 1-24-4-34z"/>'
    if style == "bun":
        return f'<circle cx="64" cy="19" r="10.5" fill="{c}"/>'
    if style == "afro":
        return f'<circle cx="64" cy="42" r="35" fill="{c}"/>'
    if style == "curly":
        blobs = [
            (42, 40, 11),
            (48, 28, 11),
            (60, 22, 12),
            (73, 23, 12),
            (84, 31, 11),
            (88, 44, 10),
            (39, 53, 8),
            (90, 56, 8),
        ]
        return "".join(f'<circle cx="{x}" cy="{y}" r="{r}" fill="{c}"/>' for x, y, r in blobs)
    return ""


def _front_hair(look: dict[str, str]) -> str:
    style, c = look["hair"], look["color"]
    if style == "side":
        hair = f'<path fill="{c}" d="M40 52c-2-20 10-32 26-32 15 0 25 11 22 32-2-8-5-13-9-16-13 3-25 2-31-2-4 4-7 10-8 18z"/>'
        if look.get("temples"):
            t = look["temples"]
            hair += (
                f'<path fill="{t}" d="M41 52c0-5 1-9 3-13l2 2c-2 3-3 7-3 11zM87 52c0-5-1-9-3-13l-2 2c2 3 3 7 3 11z"/>'
            )
        return hair
    if style == "short":
        return f'<path fill="{c}" d="M40 50c-1-19 10-30 24-30s25 11 24 30c-2-7-5-12-9-14-9 3-21 3-30 0-4 2-7 7-9 14z"/>'
    if style == "wavy_short":
        return (
            f'<path fill="{c}" d="M39 52c-3-11 1-20 8-25 3-5 10-8 17-8 8 0 15 3 19 8 6 5 9 14 6 25-2-7-5-12-8-14-3 2-7 1-10-1'
            f'-4 3-9 3-13 1-4 3-8 3-11 1-3 2-6 7-8 13z"/>'
        )
    if style == "buzz":
        return f'<path fill="{c}" opacity=".9" d="M41 49c-1-17 9-27 23-27s24 10 23 27c-2-7-5-11-8-13-10-2-20-2-30 0-3 2-6 6-8 13z"/>'
    if style == "balding":
        return f'<path fill="{c}" d="M41 54c-2-8-1-14 2-19 1 6 2 12 3 19zM87 54c2-8 1-14-2-19-1 6-2 12-3 19z"/>'
    if style == "long":
        return f'<path fill="{c}" d="M40 56c-2-22 10-34 24-34s26 12 24 34c-2-11-9-20-24-25-15 5-22 14-24 25z"/>'
    if style == "wavy":
        return f'<path fill="{c}" d="M40 56c-2-22 10-35 24-35s26 13 24 35c-4-12-12-20-20-23-8 6-22 10-28 23z"/>'
    if style in {"bob", "bob_streak"}:
        hair = f'<path fill="{c}" d="M40 58c-2-24 10-36 24-36s26 12 24 36c-3-10-7-16-12-20-10 3-22 3-27 1-4 4-7 10-9 19z"/>'
        if style == "bob_streak":
            hair += '<path fill="#C9C4BF" d="M58 24c-7 3-12 9-13 18l4-2c2-7 6-12 12-15z"/>'
        return hair
    if style == "fringe":
        return (
            f'<path fill="{c}" d="M38 76c-4-34 8-54 26-54s30 20 26 54c-2 2-5 3-8 3 3-14 2-24-1-32H47c-3 8-4 18-1 32-3 0-6-1-8-3z"/>'
            f'<path fill="{c}" d="M42 48c1-13 10-22 22-22s21 9 22 22z"/>'
        )
    if style == "ponytail":
        return (
            f'<path fill="{c}" d="M40 52c-2-20 10-31 24-31s26 11 24 31c-3-9-8-14-15-17-8 4-21 4-26 2-3 3-5 8-7 15z"/>'
        )
    if style == "bun":
        return f'<path fill="{c}" d="M40 52c-2-20 10-30 24-30s26 10 24 30c-3-8-8-14-24-16-16 2-21 8-24 16z"/>'
    if style == "afro":
        return f'<path fill="{c}" d="M40 52c0-13 10-21 24-21s24 8 24 21c-5-8-13-12-24-12s-19 4-24 12z"/>'
    if style == "curly":
        return f'<path fill="{c}" d="M41 50c1-13 10-21 23-21s22 8 23 21c-5-7-9-10-13-11-4 3-8 3-11 1-3 3-7 3-10 1-4 2-8 5-12 9z"/>'
    return ""


def _top(look: dict[str, str], skin: str, shade: str) -> str:
    top, wear = look["top"], look.get("wear", "round")
    body = f'<path fill="{top}" d="M12 128c1-22 16-33 38-36h28c22 3 37 14 38 36z"/>'
    neck = (
        f'<path fill="{skin}" d="M53 72h22v20c0 5-5 10-11 10s-11-5-11-10z"/>'
        f'<path fill="{shade}" d="M53 76c5 6 17 6 22 0v5c-5 6-17 6-22 0z" opacity=".7"/>'
    )
    if wear == "blazer":
        return (
            body + neck + '<path fill="#F5F5F2" d="M52 92l12 23 12-23-12 7z"/>'
            f'<path fill="{top}" d="M52 91l-6 5 14 27 4-8zM76 91l6 5-14 27-4-8z"/>'
            '<path fill="#000" opacity=".16" d="M52 91l-6 5 14 27 4-8zM76 91l6 5-14 27-4-8z"/>'
        )
    if wear == "shirt":
        return (
            body + neck + f'<path fill="{skin}" d="M58 92h12l-6 10z"/>'
            f'<path fill="{top}" d="M52 90l12 12-8 8-9-15zM76 90L64 102l8 8 9-15z"/>'
            '<path fill="#fff" opacity=".2" d="M52 90l12 12-8 8-9-15zM76 90L64 102l8 8 9-15z"/>'
        )
    if wear == "polo":
        return (
            body + neck + f'<path fill="{skin}" d="M60 92h8l-4 11z"/>'
            f'<path fill="{top}" d="M51 90l11 10-6 7-9-12zM77 90 66 100l6 7 9-12z"/>'
            '<path fill="#000" opacity=".14" d="M51 90l11 10-6 7-9-12zM77 90 66 100l6 7 9-12z"/>'
        )
    if wear == "coat":
        return (
            body + neck + '<path fill="#2F6F5B" d="M53 92l11 16 11-16-11 6z"/>'
            '<path fill="#DADFDB" d="M52 91l-7 5 15 30 4-14zM76 91l7 5-15 30-4-14z"/>'
        )
    return body + neck + f'<path fill="{skin}" d="M52 92c4 7 20 7 24 0-3 8-21 8-24 0z"/>'


def _face(look: dict[str, str], shade: str) -> str:
    brow = look.get("beard") or (look["color"] if look["hair"] not in {"scarf", "balding"} else "#3A2E26")
    ink = "#201A17"
    parts = [
        f'<path d="M49 47c3-2.6 8-2.6 11 0M68 47c3-2.6 8-2.6 11 0" fill="none" stroke="{brow}" stroke-width="2.3" stroke-linecap="round"/>',
        f'<circle cx="55" cy="54" r="2.5" fill="{ink}"/><circle cx="73" cy="54" r="2.5" fill="{ink}"/>',
        '<circle cx="55.8" cy="53.2" r=".8" fill="#fff"/><circle cx="73.8" cy="53.2" r=".8" fill="#fff"/>',
        f'<path d="M64 56c-1.2 4-2.4 7-2.4 8.4 0 1.2 1.2 1.7 3 1.6" fill="none" stroke="{shade}" stroke-width="1.8" stroke-linecap="round"/>',
    ]
    if look.get("beard"):
        b = look["beard"]
        parts.insert(
            0,
            f'<path fill="{b}" d="M41 58c2 7 4 11 8 13 4-2 9-3 15-3s11 1 15 3c4-2 6-6 8-13 1 16-7 28-23 28S40 74 41 58z"/>',
        )
        parts.append(f'<path fill="{b}" d="M54 66c3-2 7-2.4 10-1 3-1.4 7-1 10 1-3 1-7 1.4-10 .4-3 1-7 .6-10-.4z"/>')
        parts.append(
            '<path d="M57 71.5c4 2.8 10 2.8 14 0" fill="none" stroke="#F3E3D8" stroke-width="1.8" stroke-linecap="round"/>'
        )
    else:
        if look.get("mustache"):
            parts.append(
                f'<path fill="{look["mustache"]}" d="M53 69c3-3 8-3.6 11-2 3-1.6 8-1 11 2-4 1.4-8 1.6-11 .6-3 1-7 .8-11-.6z"/>'
            )
        parts.append(
            '<path d="M55.5 71.5c4.5 4.5 12.5 4.5 17 0-1 6.2-16 6.2-17 0z" fill="#fff" stroke="#8A3F36" stroke-width="1.4" stroke-linejoin="round"/>'
        )
    return "".join(parts)


def _glasses(color: str) -> str:
    return (
        f'<g fill="none" stroke="{color}" stroke-width="1.8"><rect x="47.5" y="48" width="15" height="12" rx="5"/>'
        '<rect x="65.5" y="48" width="15" height="12" rx="5"/><path d="M62.5 53c1-.8 2-.8 3 0M47.5 52l-5-1.2M80.5 52l5-1.2"/></g>'
    )


def portrait_svg(key: str, dept: str, name: str) -> str:
    look = LOOKS[key]
    skin, shade = SKIN[look["skin"]]
    style = look["hair"]
    bg = BACKGROUND.get(dept, "#D6DCE3")
    parts = [f'<rect width="128" height="128" fill="{bg}"/>']
    if style == "scarf":
        c = look["color"]
        parts.append(
            f'<path fill="{c}" d="M34 52c0-23 13-35 30-35s30 12 30 35c0 18-3 32-7 42 8 2 16 7 21 16H20c5-9 13-14 21-16-4-10-7-24-7-42z"/>'
        )
    parts.append(_back_hair(style, look["color"]))
    parts.append(_top(look, skin, shade))
    if style == "scarf":
        parts.append(
            f'<path fill="{look["color"]}" d="M40 80c6 10 15 15 24 15s18-5 24-15l6 16c-8 8-19 12-30 12s-22-4-30-12z"/>'
        )
    else:
        parts.append(f'<circle cx="41" cy="57" r="5" fill="{shade}"/><circle cx="87" cy="57" r="5" fill="{shade}"/>')
    parts.append(f'<ellipse cx="64" cy="54" rx="23.5" ry="28" fill="{skin}"/>')
    parts.append(_front_hair(look))
    parts.append(_face(look, shade))
    if style == "scarf":
        parts.append(
            f'<path fill="{look["color"]}" d="M38 56c-1-21 11-33 26-33s27 12 26 33c-1-11-10-23-26-23S39 45 38 56z"/>'
        )
    if look.get("glasses"):
        parts.append(_glasses(look["glasses"]))
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128" role="img" aria-label="Illustration of {name}">'
        f"{''.join(parts)}</svg>\n"
    )
