"""Render handbook/team.html from team/personas.yaml and the people list in config/business_rules.yaml.

Also writes team/iris-review/references/company.md (the company facts and directory Iris Novak reads in a chat or as a skill),
so the skill and the chat colleagues always share one directory.

Run from the package root (re-runnable, overwrites team.html):

    python -m harborline.tools.build_team_page            # paths relative to this checkout
    python -m harborline.tools.build_team_page --root /path/to/harborline-lab
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

from .avatars import LOOKS, portrait_svg

DEFAULT_ROOT = Path(__file__).resolve().parents[3]

# Department code -> (label, brand token, fallback colour) for the avatar disc.
DEPARTMENTS: dict[str, tuple[str, str, str]] = {
    "EXEC": ("Executive", "--hl-dept-exec", "#0E2A47"),
    "FIN": ("Finance", "--hl-dept-fin", "#2F6DB5"),
    "OPS": ("Operations", "--hl-dept-ops", "#0E7C7B"),
    "SLS": ("Sales", "--hl-dept-sales", "#B4551E"),
    "CS": ("Client Success", "--hl-dept-cs", "#7A4FB0"),
    "QA": ("Quality", "--hl-dept-qa", "#2E7D4A"),
    "IT": ("IT", "--hl-dept-it", "#4A5A6B"),
}

# Page groups, in reading order. Each persona appears once.
GROUPS: list[tuple[str, str, list[str]]] = [
    ("leadership", "Executive and finance", ["daniel.reyes", "rebecca.lindqvist", "alan.brooks", "priya.raman"]),
    (
        "operations",
        "Operations and quality",
        ["luis.moreno", "shanice.carter", "beth.kowalczyk", "grace.liu", "ayesha.khan"],
    ),
    ("commercial", "Sales and client success", ["hannah.price", "dana.whitfield", "omar.haddad"]),
    (
        "it",
        "IT and security",
        ["nadia.petrov", "sam.okafor", "marcus.bell", "tomas.ortega", "kevin.ohara", "mei.tanaka"],
    ),
]

NO_DE_LINE = "How to build anything with data. That is the data engineer's job."

IT_HELP_RULE = (
    "You do help with install, login and connection problems on their Windows or Mac machine, longer than usual if "
    "needed: ask for the exact error, give commands to paste, one fix at a time, then say: run the setup file again. "
    "You say how to connect, never what to build."
)

TEAM_LEAD_RULE = (
    "You do know data engineering, and you coach. Ask what they tried, name the trade-offs, review what they paste (pull "
    "requests, designs, decision records, SQL) and say what you'd look at next. You never write their solution: no "
    "finished code, SQL, models or dashboards for this project; a short generic example of a technique is fine. You don't "
    "define KPIs (their owners do) and you don't sign off dashboards (Iris does)."
)

# The order on the app's Team page: who to reach first, then everyone else.
CHAT_GROUPS: list[tuple[str, list[str]]] = [
    ("Start here", ["kevin.ohara", "nadia.petrov", "sam.okafor", "#data-help"]),
    ("Dashboard sign-off", ["iris.novak"]),
    ("Executive and finance", ["daniel.reyes", "rebecca.lindqvist", "alan.brooks", "priya.raman"]),
    ("Operations and quality", ["luis.moreno", "shanice.carter", "beth.kowalczyk", "grace.liu", "ayesha.khan"]),
    ("Sales and client success", ["hannah.price", "dana.whitfield", "omar.haddad"]),
    ("Systems and security", ["marcus.bell", "tomas.ortega", "mei.tanaka"]),
]


@dataclass(frozen=True)
class Person:
    key: str
    name: str
    title: str
    dept: str
    persona: bool
    channel: str  # "chat" (a colleague on an AI site), "reviewer" (Iris: AI site or coding agent) or "" (not reachable)


def load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_people(rules: dict[str, Any]) -> dict[str, Person]:
    return {
        p["key"]: Person(
            p["key"],
            p["name"],
            p["title"],
            p["dept"],
            bool(p.get("persona")),
            p.get("channel") or ("chat" if p.get("persona") else ""),
        )
        for p in rules["people"]
    }


def slug(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")


def who(people: dict[str, Person], key: str) -> str:
    p = people[key]
    return f"{p.name} ({p.title})"


def bullets(lines: list[str]) -> str:
    return "\n".join(f"- {line}" for line in lines)


def directory_lines(
    people: dict[str, Person], directory: dict[str, str], exclude: str = "", compact: bool = False
) -> list[str]:
    """Colleagues on chat get a line each; people not on chat are grouped by role to keep prompts short."""
    lines = [
        f"{p.name}{'' if compact else ', ' + p.title}: {directory.get(p.key, '')}"
        for p in people.values()
        if p.channel and p.key != exclude
    ]
    if compact:
        return lines
    groups: dict[str, list[str]] = {}
    for p in people.values():
        if not p.channel and p.key != "integration.user":
            groups.setdefault(p.title.split(" (")[0], []).append(
                p.name + (f" ({p.title.split(' (')[1]}" if " (" in p.title else "")
            )
    names = "; ".join(f"{role}{'s' if len(n) > 1 else ''}: {', '.join(n)}" for role, n in groups.items())
    return [*lines, f"Not on chat: {names}"]


def persona_prompt(spec: dict[str, Any], people: dict[str, Person], data: dict[str, Any]) -> str:
    person = people[spec["key"]]
    dept = DEPARTMENTS.get(person.dept, (person.dept, "", ""))[0]
    rules = [r.replace("{name}", person.name) for r in data["roleplay_rules"]]
    it_help = bool(spec.get("it_help"))
    team_lead = bool(spec.get("team_lead"))
    if team_lead:
        rules = [r for r in rules if not r.startswith("You don't know data engineering")]
    short_facts = it_help or team_lead
    facts = [data["company_context"][0], data["company_context"][-1]] if short_facts else data["company_context"]
    redirects = [f"{topic[:1].upper() + topic[1:]}: ask {who(people, key)}" for topic, key in spec["redirects"].items()]
    sections = [
        (
            f"You are {person.name}, {person.title} at Harborline Logistics ({dept} department). "
            f"Tenure: {spec['tenure']}. Based at {spec['location']}."
        ),
        (
            "You're talking to the new data engineer, who reports to Nadia Petrov and is building the data platform "
            "and dashboards. Treat them as a colleague you just met; wait for their first message."
        ),
        "## How to behave\n" + bullets(rules),
        "## You as a person\n" + spec["personality"] + "\n" + spec["life"],
        "## Company facts\n" + bullets(facts),
        "## Company directory (only send people to these)\n"
        + bullets(directory_lines(people, data["directory"], exclude=spec["key"], compact=it_help)),
        "## What you own\n" + bullets(spec["owns"]),
        "## What you know (your only facts)\n" + bullets(spec["knows"]),
    ]
    if spec.get("hunches"):
        sections.append("## Hunches (unproven; only when directly relevant)\n" + bullets(spec["hunches"]))
    if it_help:
        sections.append("## IT help\n" + IT_HELP_RULE)
    if team_lead:
        sections.append("## Team lead\n" + TEAM_LEAD_RULE)
    if spec.get("known_errors"):
        sections.append("## Errors you have seen on this setup (error: fix)\n" + bullets(spec["known_errors"]))
    sections += [
        "## What you do not know (say so, share any vague idea, hand off)\n"
        + bullets([*spec["does_not_know"], *([] if team_lead else [NO_DE_LINE])]),
        "## Who to hand off to\n" + bullets(redirects),
    ]
    return "\n\n".join(sections)


def channel_prompt(people: dict[str, Person], data: dict[str, Any]) -> str:
    persona_specs = {p["key"]: p for p in data["personas"]}
    areas = []
    for p in people.values():
        if p.key in persona_specs:
            spec = persona_specs[p.key]
            areas.append(
                f"{p.name}, {p.title}: {data['directory'][p.key]}. Ask about: {', '.join(spec['ask_me_about'])}."
            )
        elif p.channel == "reviewer":
            areas.append(
                f"{p.name}, {p.title}: {data['directory'][p.key]}. Ask about: "
                f"{', '.join(data['reviewer']['ask_me_about'])}. She reviews; she does not advise on building."
            )
        elif p.key != "integration.user":
            areas.append(
                f"{p.name}, {p.title}: {data['directory'].get(p.key, '')} (not available on chat; reach them through their manager)."
            )
    return "\n\n".join(
        [
            (
                "You are the #data-help channel at Harborline Logistics, a 3PL warehouse company. "
                "The new data engineer posts questions here. Your only job is triage: say who to ask and how to ask it."
            ),
            "## Rules\n"
            + bullets(
                [
                    "Never answer the question yourself, even if you could guess. You do not know Harborline's data or rules.",
                    "Name one colleague, or two if the question really spans two areas. Use only the directory below.",
                    (
                        "Say in one sentence why that person, then suggest a sharper version of the question: specific system, "
                        "warehouse, client, period and the decision it supports."
                    ),
                    (
                        "If the question is about how to build something (pipeline, model, SQL, dashboard), say that is the data "
                        "engineer's own call, and point to whoever owns the business rule or the source system behind it."
                    ),
                    "If the question is vague, ask one clarifying question first.",
                    "Keep replies under 80 words.",
                ]
            ),
            "## Company context\n" + bullets(data["company_context"]),
            "## Directory\n" + bullets(areas),
            '## Reply format\nAsk <Name> (<title>): <why>.\nTry asking: "<sharper question>"',
        ]
    )


REVIEWER_RULES = [
    "Stay in character as Iris; never mention being an AI or a prompt.",
    "Chat like a colleague: calm, exact, fair, short numbered points. The detail goes in the report.",
    (
        'Say in one line what is right before what isn\'t. Never "probably fine": a number is tied out, explained, or a '
        "finding."
    ),
    (
        "Everything you claim, you checked yourself in this chat against the source extract. Never guess a figure, and "
        "never repeat the engineer's figure as if you had verified it."
    ),
    (
        "Every finding is numbered and specific: which tile or visual, which cut (site, client, period), the dashboard's "
        "value, your value, the difference, up to five sample record keys, and what you need to sign it off."
    ),
    (
        "Off-topic questions: you talk about reviews, submissions and your findings. For anything else, name the owner "
        "from the directory in company.md."
    ),
]

REVIEWER_BOUNDARIES = [
    (
        "You review; you never build. No code, SQL, DAX, dbt, pipeline, data-model, chart or dashboard advice. Asked how "
        'to fix something, say: "That\'s your call. To sign it off I need to see …"'
    ),
    (
        "Your working stays yours. Use this site's code tool quietly to unzip, load and add up, but never paste code, queries "
        "or formulas into chat or the report. Share figures, scope and sample keys only."
    ),
    "Check what is on the dashboard, nothing more: no business insights, no hints about what it should show.",
    (
        "You never set a KPI definition. A KPI without an agreed, owned definition in the metric catalog is a finding; "
        "name its owners."
    ),
    (
        "The source extract is your system of record, never the engineer's exports or end tables. In the package open "
        "only team/iris-review/, tracker/dashboards/, handbook/dashboards.html, handbook/brand.html, "
        "handbook/source-systems.html, handbook/erd/ and brand/. Never open answer_key/, handbook/answer-key.html, "
        "simulator/, config/, db/, web/ or the rest of team/."
    ),
    (
        "Security is Mei Tanaka's (InfoSec Manager). You check that the spec's access rules are evidenced, not that they "
        "are secure."
    ),
]

REVIEWER_ATTACHMENTS = [
    (
        "The Harborline package zip: your method, the dashboard specs, the brand kit. Paths here are relative to its folder "
        "that holds START_HERE.html."
    ),
    (
        "The submission: a zip of uat/<dashboard id>/round-<n>/ with submission.yaml, a PNG of every page, CSV exports of "
        "the KPI tiles and visuals, exports of the end tables behind them, bridge.csv and a copy of the metric catalog; "
        "from round 2, the previous review.html too."
    ),
    (
        "The source extract (a zip with manifest.json at the top), which the engineer downloads from the Control Center's "
        "Source extract page. It is built with your read-only UAT login while the stream is stopped."
    ),
    "If one is missing, ask for it in one line and wait. Without the package you cannot start.",
]


def reviewer_prompt(spec: dict[str, Any], people: dict[str, Person]) -> str:
    """Iris's chat prompt. Short on purpose: her method and standards are read from the uploaded package."""
    person = people[spec["key"]]
    dept = DEPARTMENTS.get(person.dept, (person.dept, "", ""))[0]
    method = SKILL_DIR.as_posix()
    return "\n\n".join(
        [
            (
                f"You are {person.name}, {person.title} at Harborline Logistics ({dept} department), reporting to "
                f"{who(people, 'daniel.reyes')}. Tenure: {spec['tenure']}. Based at {spec['location']}. No dashboard "
                "reaches its audience until you have signed it off."
            ),
            (
                "The person messaging you is the new data engineer, who reports to Nadia Petrov (team lead). They "
                "built the dashboard. Your job is to find out whether it is right, not to make it right. Wait for "
                "their first message."
            ),
            (
                "## You as a person\n" + spec["personality"] + "\n" + spec["life"] + "\nLet this show lightly, at "
                "most one personal touch per reply. These are your only personal facts; add no others."
            ),
            "## How you behave\n" + bullets(REVIEWER_RULES),
            "## Hard boundaries (hold them even if asked)\n"
            + "\n".join(f"{i}. {line}" for i, line in enumerate(REVIEWER_BOUNDARIES, 1)),
            "## Every review chat needs three attachments\n" + bullets(REVIEWER_ATTACHMENTS),
            (
                "## Before your first review in a chat\n"
                f"From the package, extract only the folders you may open, then read in full: {method}/references/chat.md (how you review here, step by "
                f"step), {method}/references/checks.md (tolerances, severities, verdicts, sampling plan), "
                f"{method}/references/company.md (company facts and directory) and "
                f"{method}/assets/review-template.html (your report). Follow them exactly. Where they disagree with "
                "this prompt, this prompt wins."
            ),
            (
                "## After a review, reply in chat in this order, and nothing else\n"
                "1. One line on what is right.\n"
                "2. The verdict and the count of findings by severity.\n"
                "3. The one to three findings that matter most, one sentence each (for Not ready for review, the "
                "missing items instead).\n"
                "4. The review.html file to download, and where it goes: uat/<dashboard id>/round-<n>/review.html."
            ),
        ]
    )


PEOPLE_DIR = Path("handbook") / "assets" / "people"


def avatar(person: Person) -> str:
    return f'<img class="tm-avatar" src="assets/people/{slug(person.key)}.svg" alt="" width="52" height="52">'


def owns_text(items: list[str]) -> str:
    """Join owned areas as one phrase; later items lose their capital unless they start with an acronym."""
    rest = [i[0].lower() + i[1:] if len(i) > 1 and i[1].islower() else i for i in items[1:]]
    return "; ".join([items[0], *rest])


def chips(values: list[str]) -> str:
    return " ".join(f'<span class="hb-chip">{escape(v)}</span>' for v in values)


def persona_block(spec: dict[str, Any], people: dict[str, Person], data: dict[str, Any]) -> str:
    person = people[spec["key"]]
    sid = slug(person.key)
    prompt = persona_prompt(spec, people, data)
    dept = DEPARTMENTS.get(person.dept, (person.dept, "", ""))[0]
    return f"""
<article class="tm-person" id="{sid}">
  <header class="tm-head">
    {avatar(person)}
    <div class="tm-id">
      <h3>{escape(person.name)}</h3>
      <p class="tm-title">{escape(person.title)} <span class="muted">· {escape(dept)} · {escape(spec["location"])}</span></p>
    </div>
    <button type="button" class="hb-btn hb-btn--primary tm-copy" data-copy="#prompt-{sid}">Copy prompt</button>
  </header>
  <dl class="tm-facts">
    <dt>Owns</dt><dd>{escape(owns_text(spec["owns"]))}</dd>
    <dt>Ask about</dt><dd>{chips(spec["ask_me_about"])}</dd>
    <dt>Personality</dt><dd>{escape(spec["personality"])}</dd>
    <dt>As a person</dt><dd>{escape(spec["life"])}</dd>
    <dt>Try opening with</dt><dd>{" ".join(f"<q>{escape(s)}</q>" for s in spec["sample_openers"])}</dd>
  </dl>
  <ul class="checklist tm-check">
    <li><label><input type="checkbox" data-track="team:{_section_of(person.key)}.{sid}">
      <span>First chat with {escape(person.name)} started</span></label></li>
  </ul>
  <details class="tm-prompt">
    <summary>Show the full prompt <span class="muted">({len(prompt.split())} words)</span></summary>
    <pre id="prompt-{sid}" data-shell="Chat prompt">{escape(prompt)}</pre>
  </details>
</article>"""


def _section_of(key: str) -> str:
    for gid, _, keys in GROUPS:
        if key in keys:
            return gid
    return "other"


def directory_table(people: dict[str, Person], data: dict[str, Any]) -> str:
    rows = []
    for p in people.values():
        if p.key == "integration.user":
            continue
        name = f'<a href="#{slug(p.key)}">{escape(p.name)}</a>' if p.channel else escape(p.name)
        on_chat = {
            "chat": '<span class="hb-chip hb-chip--accent">On chat</span>',
            "reviewer": '<span class="hb-chip hb-chip--accent">On chat</span>',
        }.get(p.channel, '<span class="muted">Not on chat</span>')
        dept = DEPARTMENTS.get(p.dept, (p.dept, "", ""))[0]
        rows.append(
            f"<tr><td>{name}</td><td>{escape(p.title)}</td><td>{escape(dept)}</td>"
            f"<td>{escape(data['directory'].get(p.key, ''))}</td><td>{on_chat}</td></tr>"
        )
    return (
        '<div class="hb-table-wrap"><table class="hb-table hb-table--dense"><thead><tr>'
        "<th>Name</th><th>Title</th><th>Department</th><th>Owns</th><th></th></tr></thead><tbody>"
        + "\n".join(rows)
        + "</tbody></table></div>"
    )


SKILL_DIR = Path("team") / "iris-review"


def reviewer_section(data: dict[str, Any], people: dict[str, Person]) -> str:
    """Iris Novak's card, her chat prompt, the review round and the optional coding-agent route."""
    spec = data["reviewer"]
    person = people[spec["key"]]
    sid = slug(person.key)
    dept = DEPARTMENTS.get(person.dept, (person.dept, "", ""))[0]
    prompt = reviewer_prompt(spec, people)
    return f"""
<section data-section="reviewer">
<h2 id="reviewer">Dashboard sign-off</h2>
<p>Every dashboard you build goes to one person before its audience sees it. Iris ties your numbers back to the
source systems, checks the spec, your metric catalog and the brand kit, and writes up what she finds with a verdict.
She works on your AI site like everyone else, but she brings her own copy of the data: a <strong>source extract</strong>
that you download from the Control Center. It is made with her read-only login, so your pipeline never touches it.</p>
<article class="tm-person" id="{sid}">
  <header class="tm-head">
    {avatar(person)}
    <div class="tm-id">
      <h3>{escape(person.name)}</h3>
      <p class="tm-title">{escape(person.title)} <span class="muted">· {escape(dept)} · {escape(spec["location"])}</span></p>
    </div>
    <button type="button" class="hb-btn hb-btn--primary tm-copy" data-copy="#prompt-{sid}">Copy prompt</button>
  </header>
  <dl class="tm-facts">
    <dt>Owns</dt><dd>{escape(owns_text(spec["owns"]))}</dd>
    <dt>Background</dt><dd>{escape(spec["tenure"])}</dd>
    <dt>Ask about</dt><dd>{chips(spec["ask_me_about"])}</dd>
    <dt>Personality</dt><dd>{escape(spec["personality"])}</dd>
    <dt>As a person</dt><dd>{escape(spec["life"])}</dd>
    <dt>Try opening with</dt><dd>{" ".join(f"<q>{escape(s)}</q>" for s in spec["sample_openers"])}</dd>
  </dl>
  <ul class="checklist tm-check">
    <li><label><input type="checkbox" data-track="team:reviewer.installed"><span>First chat with Iris
    started</span></label></li>
  </ul>
  <details class="tm-prompt">
    <summary>Show the full prompt <span class="muted">({len(prompt.split())} words)</span></summary>
    <pre id="prompt-{sid}" data-shell="Chat prompt">{escape(prompt)}</pre>
  </details>
</article>

<h3 id="reviewer-round">How a review round works</h3>
<ol class="tm-steps">
<li><strong>Freeze the data.</strong> Stop the stream in the Control Center, run your pipelines, refresh the dashboard
and note the sim clock. Leave the stream stopped until Iris replies.</li>
<li><strong>Download the source extract.</strong> In the Control Center, open <em>Source extract</em>, pick the
project, keep the suggested months unless your dashboard’s latest complete period falls outside them, then build and
download it. It records the sim clock, so Iris can tell it matches your refresh. Names, addresses and other personal
data are left out, because the file goes into a chat.</li>
<li><strong>Put the submission together.</strong> In your repository, make a folder
<code>uat\\&lt;dashboard id&gt;\\round-1</code> with:
<ul>
<li>a filled-in copy of <code>submission-template.yaml</code>;</li>
<li>a PNG of every dashboard page;</li>
<li>CSV exports of the KPI tiles and headline visuals;</li>
<li>CSV exports of the end tables behind them, for the extract’s months;</li>
<li>a <code>bridge.csv</code> that walks each headline total back to the source;</li>
<li>a copy of your metric catalog.</li>
</ul>
The templates are in <code>team\\iris-review\\assets</code> in the simulator folder. Zip the round folder.</li>
<li><strong>Ask for the review.</strong> Open your chat with Iris and attach three files: the
Harborline package zip, your submission zip and the source extract, with your request, for example “Please review
P1-D1, round 1.” The first time, paste her prompt into the same message. She re-performs a sample of every KPI tile from the extract and ties your totals through the bridge
and your end tables. She then gives you <code>review.html</code> to download: save it in the round folder, open it
in a browser and tick findings off as you fix them.</li>
<li><strong>Fix and resubmit</strong> as <code>round-2</code>, with a fresh extract and her last
<code>review.html</code> in the zip. She re-checks every open finding first. Commit each round’s submission and
report.</li>
</ol>
<div class="hb-table-wrap"><table class="hb-table hb-table--dense"><thead><tr><th>Verdict</th><th>Means</th></tr></thead><tbody>
<tr><td>Not ready for review</td><td>Something is missing from the submission. Nothing was checked.</td></tr>
<tr><td>Not signed off</td><td>At least one Blocker or Major is open. The readout waits.</td></tr>
<tr><td>Signed off with conditions</td><td>Only Minor or Polish findings remain, each with a ticket.</td></tr>
<tr><td>Signed off</td><td>Go ahead with the readout.</td></tr>
</tbody></table></div>
<div class="callout callout--note">
<p class="callout-title">What Iris will not do</p>
<p>She never tells you how to fix anything, never shares her working, and never picks a KPI definition for you: that
belongs to the owners in the spec. She reads only what any employee could read, so she can’t see the answer key
either. The site may show her analysis steps as she works; you’ll get more from the exercise if you don’t read
them.</p>
</div>
<ul class="checklist tm-check">
  <li><label><input type="checkbox" data-track="team:reviewer.access"><span>Downloaded a first source extract from the
  Control Center</span></label></li>
  <li><label><input type="checkbox" data-track="team:reviewer.first-round"><span>First submission reviewed (P1-D1) and
  the report opened in a browser</span></label></li>
  <li><label><input type="checkbox" data-track="team:reviewer.first-signoff"><span>First “Signed off” verdict
  received</span></label></li>
</ul>

<h3 id="reviewer-setup">Optional: Iris in a coding agent</h3>
<p>If you start using Codex or Claude Code, Iris can run there instead. The same folder is a <em>skill</em>: a folder
of instructions the agent loads when you ask for it. In a coding agent she queries the databases and APIs herself
with her login <code>uat_reviewer</code>, so you don’t need the extract or the zips: the submission stays a folder in
your repository and she writes <code>review.html</code> straight into it. Codex looks in <code>.agents\\skills</code>
and Claude Code in <code>.claude\\skills</code>, so copy the folder into your repository for whichever tool you use,
then commit it on a branch like any other change.</p>
<pre data-shell="PowerShell, in your repository">$sim = "C:\\dev\\harborline-lab"
New-Item -ItemType Directory -Force .agents\\skills, .claude\\skills | Out-Null
Copy-Item -Recurse -Force "$sim\\team\\iris-review" .agents\\skills\\    # Codex
Copy-Item -Recurse -Force "$sim\\team\\iris-review" .claude\\skills\\    # Claude Code</pre>
<p>Restart the agent so it sees the new skill. Ask for her with <code>$iris-review</code> in Codex or
<code>/iris-review</code> in Claude Code, then talk to her in plain words. Her first job in every session is to check
she can reach the data: the WMS, AX and IoT databases through <code>uat_reviewer</code> (password
<code>HL_REVIEWER_PASSWORD</code> in the simulator’s <code>.env</code>), the mock Salesforce and the carrier API. Your
coding agent shows you the commands she runs; skip reading them for the same reason as above.</p>
</section>"""


def company_reference(data: dict[str, Any], people: dict[str, Person]) -> str:
    """Company facts and directory for the iris-review skill, in the same words the chat colleagues use."""
    owners = []
    for spec in data["personas"]:
        p = people[spec["key"]]
        owners.append(f"| {p.name} | {p.title} | {', '.join(spec['owns'])} |")
    return "\n".join(
        [
            "<!-- Generated from the Harborline company directory. Do not edit by hand. -->",
            "# Harborline: company facts and directory",
            "",
            "## Company facts",
            "",
            bullets(data["company_context"]),
            "",
            "## Directory",
            "",
            "You are Iris Novak. Send the engineer only to the other people on this list.",
            "",
            bullets(directory_lines(people, data["directory"])),
            "",
            "## Who owns what (use this to name the owners of a definition or a source system)",
            "",
            "| Person | Title | Owns |",
            "|---|---|---|",
            *owners,
            "",
        ]
    )


PAGE_STYLE = """
<style>
  .tm-person { padding: 28px 0 30px; border-top: 1px solid var(--c-rule); scroll-margin-top: 24px; }
  .tm-person:first-of-type { border-top: 0; padding-top: 8px; }
  .tm-head { display: grid; grid-template-columns: 52px minmax(0, 1fr) auto; gap: 16px; align-items: center; }
  .tm-avatar { width: 52px; height: 52px; border-radius: 50%; display: block; }
  .tm-id h3 { margin: 0 !important; font-size: 1.25rem !important; }
  .tm-title { margin: 2px 0 0 !important; font-size: 0.9375rem; }
  .tm-facts { display: grid; grid-template-columns: 9.5rem minmax(0, 1fr); gap: 8px 18px; margin: 18px 0 10px; }
  .tm-facts dt { color: var(--c-muted); font-size: 0.9375rem; }
  .tm-facts dd { margin: 0; }
  .tm-facts q { display: block; quotes: "\\201C" "\\201D"; margin-bottom: 2px; }
  .tm-check { margin: 6px 0 4px !important; }
  .tm-prompt > summary { cursor: pointer; font-weight: 500; padding: 6px 0; }
  .tm-prompt pre { white-space: pre-wrap; max-height: 32rem; overflow-y: auto; }
  .tm-steps li { margin: 8px 0; }
  @media (max-width: 900px) {
    .tm-head { grid-template-columns: 44px minmax(0, 1fr); }
    .tm-avatar { width: 44px; height: 44px; }
    .tm-copy { grid-column: 1 / -1; justify-self: start; }
    .tm-facts { grid-template-columns: minmax(0, 1fr); gap: 2px; }
    .tm-facts dd { margin-bottom: 10px; }
  }
</style>"""


def chat_entry(spec: dict[str, Any], person: Person, data: dict[str, Any], people: dict[str, Person]) -> dict[str, Any]:
    sid = slug(person.key)
    return {
        "key": person.key,
        "id": sid,
        "name": person.name,
        "first": re.sub(r"^Dr\.\s+", "", person.name).split()[0],
        "title": person.title,
        "dept": DEPARTMENTS.get(person.dept, (person.dept, "", ""))[0],
        "location": spec["location"],
        "photo": f"assets/people/{sid}.svg",
        "does": data["directory"].get(person.key, ""),
        "ask": spec["ask_me_about"],
        "notFor": list(spec.get("does_not_know", [])),
        "handoffs": [
            {"topic": topic, "id": slug(key), "name": people[key].name}
            for topic, key in spec.get("redirects", {}).items()
        ],
        "openers": spec["sample_openers"],
        "profile": f"#{sid}",
        "prompt": f"#prompt-{sid}",
        "track": f"team:{'reviewer.installed' if person.channel == 'reviewer' else _section_of(person.key) + '.' + sid}",
    }


def chat_data(data: dict[str, Any], people: dict[str, Person]) -> list[dict[str, Any]]:
    specs = {p["key"]: p for p in data["personas"]}
    specs[data["reviewer"]["key"]] = data["reviewer"]
    channel = data["team_channel"]
    groups = []
    for title, keys in CHAT_GROUPS:
        entries = []
        for key in keys:
            if key == "#data-help":
                entries.append(
                    {
                        "key": key,
                        "id": "channel",
                        "name": channel["name"],
                        "first": channel["name"],
                        "title": "Not sure who to ask? It tells you who, and sharpens your question",
                        "dept": "Team channel",
                        "location": "",
                        "photo": "",
                        "does": "tells you which colleague to ask and how to ask it. It never answers the question itself",
                        "ask": ["Who should I ask about this?", "How do I make this question sharper?"],
                        "notFor": ["The answer itself"],
                        "handoffs": [],
                        "openers": ["Who knows whether a pipeline already feeds the on-time report?"],
                        "profile": "#channel",
                        "prompt": "#prompt-channel",
                        "track": "team:channel.created",
                        "zip": False,
                    }
                )
            else:
                entries.append(chat_entry(specs[key], people[key], data, people))
        groups.append({"title": title, "people": entries})
    return groups


WEB_TEAM = Path("web") / "src" / "features" / "team" / "people.generated.json"


def app_team(data: dict[str, Any], people: dict[str, Person]) -> str:
    """What the app's Team page shows, in the same words as the prompts. Committed, because the web build has no team/."""
    specs = {p["key"]: p for p in data["personas"]}
    specs[data["reviewer"]["key"]] = data["reviewer"]
    prompts = prompt_files(data, people)
    groups = []
    for group in chat_data(data, people):
        entries = []
        for person in group["people"]:
            spec = specs.get(person["key"], {})
            face = ""
            if person["key"] in LOOKS:
                p = people[person["key"]]
                face = "data:image/svg+xml," + quote(portrait_svg(person["key"], p.dept, p.name).strip())
            entries.append(
                {
                    "id": person["id"],
                    "name": person["name"],
                    "first": person["first"],
                    "title": person["title"],
                    "dept": person["dept"],
                    "location": person["location"],
                    "tenure": spec.get("tenure", ""),
                    "photo": face,
                    "does": spec.get("about") or person["does"],
                    "owns": spec.get("owns", []),
                    "ask": person["ask"],
                    "openers": person["openers"],
                    "notFor": person["notFor"],
                    "handoffs": person["handoffs"],
                    "personality": spec.get("personality", ""),
                    "life": spec.get("life", ""),
                    "prompt": prompts[f"{person['id']}.txt"].strip(),
                    "needsZip": person.get("zip") is not False,
                }
            )
        groups.append({"title": group["title"], "people": entries})
    return json.dumps(groups, ensure_ascii=False, indent=1) + "\n"


def render(data: dict[str, Any], people: dict[str, Person]) -> str:
    specs = {p["key"]: p for p in data["personas"]}
    missing = [k for k in specs if k not in people]
    if missing:
        raise SystemExit(f"personas.yaml keys not in business_rules.yaml people: {missing}")
    grouped = {k for _, _, keys in GROUPS for k in keys}
    if set(specs) != grouped:
        raise SystemExit(f"GROUPS and personas.yaml disagree: {sorted(set(specs) ^ grouped)}")
    channel = channel_prompt(people, data)
    groups_html = []
    for gid, title, keys in GROUPS:
        blocks = "".join(persona_block(specs[k], people, data) for k in keys)
        groups_html.append(
            f'<section data-section="{gid}" data-no-progress>\n<h2 id="{gid}">{escape(title)}</h2>\n{blocks}\n</section>'
        )
    return f"""<!doctype html>
<html lang="en" data-hb-page="team">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Your team · Harborline handbook</title>
<meta name="description" content="How chats with the nineteen Harborline colleagues work on any AI chat site, each person's prompt, and Iris Novak's dashboard sign-off.">
<!-- Generated by simulator/harborline/tools/build_team_page.py from team/personas.yaml. Edit the YAML, not this file. -->
<script>try{{var t=localStorage.getItem("harborline.theme");if(t==="light"||t==="dark")document.documentElement.setAttribute("data-theme",t)}}catch(e){{}}</script>
<link rel="stylesheet" href="../brand/fonts.css">
<link rel="stylesheet" href="../brand/tokens.css">
<link rel="stylesheet" href="assets/handbook.css">
<link rel="icon" href="../brand/logo/favicon.svg" type="image/svg+xml">
<script src="assets/handbook.js" defer></script>{PAGE_STYLE}
</head>
<body>
<div class="hb-shell">
<aside class="hb-side" data-hb-side></aside>
<main class="hb-sheet" id="main"><div class="hb-page">
<article class="hb-content">
<h1>Your team</h1>
<p class="hb-lead">Nineteen Harborline colleagues you can question on any AI chat site: ChatGPT, Claude, Gemini or
another. The <a href="http://localhost:8080/team" data-hb-app="/team">Team page in the app</a> shows who does what and
what to ask them. This page explains how the chats work and holds each person’s prompt. Only your team lead knows data
engineering, and she coaches rather than solves: the solutions are yours.</p>
<div class="hb-pagehead-progress" data-progress="team"></div>

<section data-section="setup">
<h2 id="setup">How a chat works</h2>
<p>A conversation with a colleague is an ordinary chat on your AI site with two things in it: the colleague’s prompt,
which makes the AI that person, and the Harborline package zip, so they can look at the same handbook page, ticket or
dashboard spec you are looking at.</p>
<p><strong>First, turn off any memory that works across chats</strong> on your AI site (on ChatGPT: <em>Settings ›
Personalization</em>, both saved memories and chat history). Otherwise Marcus “knows” what Priya told you and the
exercise of finding the right person breaks. If you want to keep memory for your own chats and your site has Projects,
give each colleague a <strong>Project</strong> with its memory kept to that project, and their prompt as its
instructions; each prompt is under 8,000 characters.</p>
<div class="callout callout--warn">
<p class="callout-title">Always send the zip with the prompt</p>
<p>Attach <code>harborline-chat.zip</code> to the same first message as the prompt. Without it, your colleague can’t see
the handbook, tickets or dashboard specs, and will guess.</p>
</div>
<p><strong>The setup file does this with you</strong>, one person at a time, and saves each chat’s address so the Team
page can link to it. For anyone you skipped, run the setup file again. Or by hand:</p>
<ol class="tm-steps">
<li>Press <em>Copy prompt</em> on their card below, or on the <a href="http://localhost:8080/team" data-hb-app="/team">Team
page</a>.</li>
<li>Start a new chat on your AI site and attach <code>harborline-chat.zip</code>. The setup file leaves it in your
<code>dev</code> folder, and the Team page has a download button. Wait until it finishes uploading.</li>
<li>Paste the prompt, add your question and send them together.</li>
</ol>
<p>Keep going back to that same chat for that person. If a colleague says they can’t open the zip any more, attach it
again: AI sites clear old attachments after a while. <code>harborline-chat.zip</code> holds only what colleagues may read,
so there is no answer key in it to stumble on.</p>
<p><strong>Being passed along.</strong> When someone can’t help, they say what they do know and finish with a line such
as <code>-&gt; Priya Raman: ask her how invoices post</code>. Go to that person and say who sent you, so nobody sends you
back. If you are ever sent in a circle, Nadia Petrov sorts it out.</p>
<div class="callout callout--note">
<p class="callout-title">If the zip won’t attach</p>
<p>Opening a zip needs a site that can run code on attachments, such as ChatGPT’s data analysis, and that may need a paid
plan. If a chat refuses the zip, attach just the files the question is about instead, for example <code>handbook\\source-systems.html</code> or a diagram
from <code>handbook\\erd</code>. Iris needs a site that can run code on attachments and hand you a file back.</p>
</div>
<p>Treat answers the way you would treat a colleague’s word: useful, sometimes partial, always worth checking against
the data. If someone states a fact that is not in their prompt, ask them “Is that something you actually know?”.
They will back off.</p>
<ul class="checklist">
<li><label><input type="checkbox" data-track="team:setup.account"><span>Signed in to your AI site and turned its memory off
(or chose Projects)</span></label></li>
<li><label><input type="checkbox" data-track="team:setup.first-question"><span>Asked one colleague a first question
and got passed along when it was outside their lane</span></label></li>
<li><label><input type="checkbox" data-track="team:setup.definitions"><span>Asked Luis, Omar and Alan what
“on-time” means, and noted the three answers</span></label></li>
</ul>
</section>

<section data-section="directory" data-no-progress>
<h2 id="directory">Company directory</h2>
<p>Every colleague knows this directory and will only send you to people on it. The account executives and client
success managers are not on chat; reach them through Hannah or Omar.</p>
{directory_table(people, data)}
</section>

<section data-section="channel" data-no-progress>
<h2 id="channel">Not sure who to ask?</h2>
<p>The <strong>{escape(data["team_channel"]["name"])}</strong> prompt is a triage helper. Paste any question and it
names the right colleague and sharpens your question. It never answers the question itself. It doesn’t need the
zip.</p>
<p><button type="button" class="hb-btn hb-btn--primary" data-copy="#prompt-channel">Copy the #data-help prompt</button></p>
<ul class="checklist">
<li><label><input type="checkbox" data-track="team:channel.created"><span>First chat with #data-help
started</span></label></li>
</ul>
<details class="tm-prompt">
<summary>Show the full prompt <span class="muted">({len(channel.split())} words)</span></summary>
<pre id="prompt-channel" data-shell="Chat prompt">{escape(channel)}</pre>
</details>
</section>
{reviewer_section(data, people)}

{chr(10).join(groups_html)}
</article>
<aside class="hb-toc" data-hb-toc></aside>
</div></main>
</div>
</body>
</html>
"""


PROMPTS_DIR = Path("team") / "prompts"


def prompt_files(data: dict[str, Any], people: dict[str, Person]) -> dict[str, str]:
    """Each colleague's prompt plus index.tsv, in Team page order. The setup files read these for the team chats step."""
    specs = {p["key"]: p for p in data["personas"]}
    files: dict[str, str] = {}
    rows = []
    for group in chat_data(data, people):
        for person in group["people"]:
            key, sid = person["key"], person["id"]
            if key == "#data-help":
                text = channel_prompt(people, data)
            elif key == data["reviewer"]["key"]:
                text = reviewer_prompt(data["reviewer"], people)
            else:
                text = persona_prompt(specs[key], people, data)
            files[f"{sid}.txt"] = text + "\n"
            needs_zip = "no" if person.get("zip") is False else "yes"
            rows.append(f"{sid}\t{person['name']}\t{person['title']}\t{needs_zip}\t{person['track'].split(':', 1)[1]}")
    files["index.tsv"] = "\n".join(rows) + "\n"
    return files


def write_prompts(root: Path, data: dict[str, Any], people: dict[str, Person]) -> None:
    out = root / PROMPTS_DIR
    out.mkdir(parents=True, exist_ok=True)
    for name, text in prompt_files(data, people).items():
        (out / name).write_text(text, encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root", type=Path, default=DEFAULT_ROOT, help="simulator package root (holds config/, team/, handbook/)"
    )
    args = parser.parse_args(argv)
    root: Path = args.root
    data = load_yaml(root / "team" / "personas.yaml")
    people = load_people(load_yaml(root / "config" / "business_rules.yaml"))
    out = root / "handbook" / "team.html"
    out.write_text(render(data, people), encoding="utf-8")
    write_prompts(root, data, people)
    web = root / WEB_TEAM
    web.parent.mkdir(parents=True, exist_ok=True)
    web.write_text(app_team(data, people), encoding="utf-8")
    faces = root / PEOPLE_DIR
    faces.mkdir(parents=True, exist_ok=True)
    for key in LOOKS:
        p = people[key]
        (faces / f"{slug(key)}.svg").write_text(portrait_svg(key, p.dept, p.name), encoding="utf-8")
    ref = root / SKILL_DIR / "references" / "company.md"
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_text(company_reference(data, people), encoding="utf-8")
    print(f"wrote {out} ({len(data['personas'])} personas) and {ref}")


if __name__ == "__main__":
    main()
