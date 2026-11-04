"""The team page, the chat prompts and Iris Novak's review prompt and skill all come from team/personas.yaml."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from harborline.tools import build_team_page as b

ROOT = b.DEFAULT_ROOT
SKILL = ROOT / b.SKILL_DIR
PROJECT_INSTRUCTIONS_LIMIT = 8000  # ChatGPT's, the tightest we know of


def _inputs() -> tuple[dict, dict[str, b.Person]]:
    data = b.load_yaml(ROOT / "team" / "personas.yaml")
    return data, b.load_people(b.load_yaml(ROOT / "config" / "business_rules.yaml"))


def test_generated_files_match_the_yaml() -> None:
    data, people = _inputs()
    assert (ROOT / "handbook" / "team.html").read_text(encoding="utf-8") == b.render(data, people), (
        "handbook/team.html is stale: run python -m harborline.tools.build_team_page"
    )
    assert (SKILL / "references" / "company.md").read_text(encoding="utf-8") == b.company_reference(data, people)


def test_every_prompt_fits_project_instructions() -> None:
    data, people = _inputs()
    prompts = {s["key"]: b.persona_prompt(s, people, data) for s in data["personas"]}
    prompts["#data-help"] = b.channel_prompt(people, data)
    prompts["iris"] = b.reviewer_prompt(data["reviewer"], people)
    too_long = {k: len(v) for k, v in prompts.items() if len(v) >= PROJECT_INSTRUCTIONS_LIMIT}
    assert not too_long


def test_iris_is_in_every_directory_and_reviews_from_her_own_prompt() -> None:
    data, people = _inputs()
    iris = people[data["reviewer"]["key"]]
    assert iris.channel == "reviewer" and not iris.persona
    assert iris.key not in {s["key"] for s in data["personas"]}
    line = f"{iris.name}"
    assert all(line in b.persona_prompt(s, people, data) for s in data["personas"])
    assert "She reviews; she does not advise" in b.channel_prompt(people, data)
    assert all(key in people for spec in data["personas"] for key in spec["redirects"].values())
    prompt = b.reviewer_prompt(data["reviewer"], people)
    for ref in re.findall(r"(team/iris-review/[\w./-]+\.(?:md|html))", prompt):
        assert (ROOT / ref).is_file(), ref
    assert "references/chat.md" in prompt and "source extract" in prompt


def test_the_package_zip_never_opens_the_answer_key() -> None:
    data, people = _inputs()
    for spec in data["personas"]:
        prompt = b.persona_prompt(spec, people, data)
        allowed = prompt.split("package zip")[1].split("Never")[0]
        assert "handbook/" in allowed and "answer_key/" not in allowed
        assert "answer_key/" in prompt.split("package zip")[1].split("Never")[1]
    iris_rules = " ".join(b.REVIEWER_BOUNDARIES)
    assert "answer_key/" not in iris_rules.split("Never open")[0]
    method = (SKILL / "references" / "chat.md").read_text(encoding="utf-8")
    allowed = method.split("**Never open:**")[0]
    assert "answer_key" not in allowed and "answer-key" not in allowed


def test_skill_is_valid_for_codex_and_claude_code() -> None:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    front = yaml.safe_load(text.split("---")[1])
    assert front["name"] == SKILL.name == "iris-review"
    assert 0 < len(front["description"]) <= 1024
    assert len(text.splitlines()) < 500
    for ref in re.findall(r"`((?:references|assets)/[\w./-]+)`", text):
        assert (SKILL / ref).is_file(), ref


def test_report_template_carries_valid_example_json() -> None:
    import json

    html = (SKILL / "assets" / "review-template.html").read_text(encoding="utf-8")
    m = re.search(r'<script type="application/json" id="review-data">(.*?)</script>', html, re.DOTALL)
    assert m
    data = json.loads(m.group(1))
    assert data["example"] is True
    assert data["verdict"] in {"not_ready", "not_signed_off", "signed_off_with_conditions", "signed_off"}
    assert {f["severity"] for f in data["findings"]} <= {"blocker", "major", "minor", "polish"}


def test_skill_never_points_at_the_answer_key_as_a_source() -> None:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    allowed = text.split("**Never open:**")[0]
    assert "answer_key" not in allowed and "answer-key" not in allowed
    checks = Path(SKILL / "references" / "checks.md").read_text(encoding="utf-8")
    assert "answer_key" not in checks and "answer-key" not in checks
