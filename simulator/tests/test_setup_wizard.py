"""The two setup files, the new names, and the colleague rules that go with them."""

from __future__ import annotations

import io
import json
import re
import shutil
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from harborline.api import control
from harborline.settings import load_settings, reset_settings
from harborline.tools import avatars
from harborline.tools import build_chat_zip as z
from harborline.tools import build_known_issues as k
from harborline.tools import build_team_page as b

ROOT = b.DEFAULT_ROOT
WINDOWS = ROOT / "SETUP-WINDOWS.cmd"
MAC = ROOT / "SETUP-MAC.command"
OLD_NAMES = re.compile(r"harborline-simulator|harborline-3pl-simulator|harborline-data-platform")
# Only these may mention the old names: they tell people not to use them.
MAY_MENTION_OLD_NAMES = {
    "SETUP-WINDOWS.cmd",
    "SETUP-MAC.command",
    "team/personas.yaml",
    "handbook/team.html",
    "handbook/getting-started.html",
    "team/prompts/kevin-ohara.txt",
    "web/src/features/team/people.generated.json",
}


def _inputs() -> tuple[dict, dict[str, b.Person]]:
    return b.load_yaml(ROOT / "team" / "personas.yaml"), b.load_people(
        b.load_yaml(ROOT / "config" / "business_rules.yaml")
    )


def test_windows_file_is_one_ascii_crlf_file_that_cmd_can_read() -> None:
    raw = WINDOWS.read_bytes()
    assert raw.isascii()
    assert raw.count(b"\n") == raw.count(b"\r\n"), "cmd.exe needs CRLF"
    assert raw.startswith(b"<# :")
    assert b"#>" in raw.split(b"\r\n$ErrorActionPreference", 1)[0]


def test_mac_file_is_executable_lf_and_parses_in_the_bash_macos_ships() -> None:
    assert MAC.stat().st_mode & stat.S_IXUSR
    assert b"\r" not in MAC.read_bytes()
    subprocess.run(["/bin/bash", "-n", str(MAC)], check=True)
    text = MAC.read_text(encoding="utf-8")
    for bash4 in ("declare -A", "mapfile", "readarray", ",,}", "^^}"):
        assert bash4 not in text


def test_step_counters_match_the_steps() -> None:
    win = WINDOWS.read_text(encoding="ascii")
    assert int(re.search(r"StepTotal = (\d+)", win).group(1)) == len(re.findall(r"(?m)^Step ['\"]", win))
    mac = MAC.read_text(encoding="utf-8")
    assert int(re.search(r"(?m)^STEP_TOTAL=(\d+)", mac).group(1)) == len(re.findall(r"(?m)^step ", mac))


def test_both_files_use_the_same_names_and_tell_people_about_kevin() -> None:
    for text in (WINDOWS.read_text(encoding="ascii"), MAC.read_text(encoding="utf-8")):
        assert "harborline-lab" in text and "harborline-analytics" in text
        assert "Kevin O" in text and "again" in text
        assert "DBeaver.DBeaver.Community" in text or "dbeaver-community" in text
        assert "update-shell" in text, "pre-commit must end up on PATH"
        assert "--web" in text, "never the paste-a-token route"


def test_the_old_names_are_gone() -> None:
    skip = {".venv", "node_modules", "dist", "__pycache__", ".pytest_cache", ".ruff_cache", "landing", ".claude"}
    offenders = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or skip & set(path.parts) or path.suffix in {".png", ".xlsx", ".gz", ".woff2", ".pyc"}:
            continue
        rel = path.relative_to(ROOT).as_posix()
        if rel in MAY_MENTION_OLD_NAMES or rel.endswith("test_setup_wizard.py") or ".egg-info" in rel:
            continue
        if OLD_NAMES.search(path.read_text(encoding="utf-8", errors="ignore")):
            offenders.append(rel)
    assert not offenders


def test_compose_project_cannot_collide_with_the_earlier_attempt() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert re.search(r"(?m)^name: harborline-lab$", compose)
    assert "image: harborline-lab:local" in compose


def test_repo_template_is_complete() -> None:
    template = ROOT / "setup" / "repo-template"
    for name in (
        ".gitignore",
        ".gitattributes",
        ".pre-commit-config.yaml",
        ".sqlfluff",
        "README.md",
        "CHANGELOG.md",
        ".github/pull_request_template.md",
    ):
        assert (template / name).is_file(), name
    assert ".env" in (template / ".gitignore").read_text(encoding="utf-8")


def test_every_colleague_hands_off_and_never_loops() -> None:
    data, people = _inputs()
    for spec in data["personas"]:
        prompt = b.persona_prompt(spec, people, data)
        assert "-> <name>: what to ask them" in prompt, spec["key"]
        assert "never send them back" in prompt, spec["key"]
        assert "Kevin O'Hara" in prompt or spec["key"] == "kevin.ohara"


def test_kevin_knows_every_error_seen_on_real_installs() -> None:
    data, people = _inputs()
    kevin = next(s for s in data["personas"] if s["key"] == "kevin.ohara")
    prompt = b.persona_prompt(kevin, people, data)
    for text in (
        "docker_engine",
        "HTTP 401 Bad credentials",
        "No package found",
        "DBeaver.DBeaver.Community",
        "no configuration file provided",
        "SQLITE_ERROR",
        'relation "dbo.custtable"',
        "cross-database references",
        "Not recognized or command not found",
        "uv tool update-shell",
        "a same-named folder",
        "db ax2012, schema dbo",
    ):
        assert text in prompt, text
    assert "never what to build" in prompt


def test_everyone_on_chat_has_a_portrait_and_a_place_in_team_chat() -> None:
    _, people = _inputs()
    on_chat = {p.key for p in people.values() if p.channel}
    assert on_chat == set(avatars.LOOKS)
    listed = {key for _, keys in b.CHAT_GROUPS for key in keys}
    assert on_chat | {"#data-help"} == listed
    for key in on_chat:
        svg = ROOT / b.PEOPLE_DIR / f"{b.slug(key)}.svg"
        assert svg.read_text(encoding="utf-8") == avatars.portrait_svg(key, people[key].dept, people[key].name)


def test_team_page_lives_in_the_app_and_matches_the_yaml() -> None:
    data, people = _inputs()
    html = (ROOT / "handbook" / "team.html").read_text(encoding="utf-8")
    for group in b.chat_data(data, people):
        for person in group["people"]:
            assert f'id="{person["prompt"][1:]}"' in html, person["key"]
            assert f'data-track="{person["track"]}"' in html, person["key"]
    generated = (ROOT / b.WEB_TEAM).read_text(encoding="utf-8")
    assert generated == b.app_team(data, people), "stale: run python -m harborline.tools.build_team_page"
    groups = json.loads(generated)
    entries = {p["id"]: p for g in groups for p in g["people"]}
    assert len(entries) == 20 and groups[0]["people"][0]["id"] == "kevin-ohara"
    nadia = entries["nadia-petrov"]
    assert nadia["title"] == "Data Engineering Team Lead" and nadia["ask"] and nadia["handoffs"] and nadia["photo"]
    assert "You are Nadia Petrov" in nadia["prompt"]
    assert all(h["id"] in entries for p in entries.values() for h in p["handoffs"])
    # Team moved out of the handbook: the page is the app's, and nothing points at the old widget.
    assert "Team chat" not in html and 'id="tc-data"' not in html and 'data-hb-app="/team"' in html
    assert not (ROOT / "handbook" / "assets" / "team-chat.js").exists()
    app = (ROOT / "web" / "src" / "App.tsx").read_text(encoding="utf-8")
    assert "path: 'team/*'" in app
    for rel in ("SETUP-WINDOWS.cmd", "SETUP-MAC.command", "START_HERE.html", "handbook/getting-started.html"):
        assert "Team chat" not in (ROOT / rel).read_text(encoding="utf-8"), rel


def test_progress_totals_match_the_pages() -> None:
    script = (ROOT / "handbook" / "assets" / "handbook.js").read_text(encoding="utf-8")
    for page, path in (
        ("start", "START_HERE.html"),
        ("getting-started", "handbook/getting-started.html"),
        ("team", "handbook/team.html"),
    ):
        total = int(re.search(rf'id: "{page}".*?total: (\d+)', script).group(1))
        tracked = set(re.findall(rf'data-track="{page}:([^"]+)"', (ROOT / path).read_text(encoding="utf-8")))
        assert total == len(tracked), page


def test_nadia_coaches_but_never_builds_and_is_the_last_stop() -> None:
    data, people = _inputs()
    nadia = next(s for s in data["personas"] if s["key"] == "nadia.petrov")
    prompt = b.persona_prompt(nadia, people, data)
    assert "You do know data engineering" in prompt and "never write their solution" in prompt
    assert "You don't know data engineering" not in prompt and b.NO_DE_LINE not in prompt
    for spec in data["personas"]:
        other = b.persona_prompt(spec, people, data)
        assert "or Nadia Petrov if nobody's left" in other
        if spec["key"] != "nadia.petrov":
            assert "reports to Nadia Petrov" in other
            assert "You do know data engineering" not in other
            assert "You don't know data engineering" in other


def test_prompt_files_match_the_page_for_every_colleague() -> None:
    data, people = _inputs()
    files = b.prompt_files(data, people)
    folder = ROOT / b.PROMPTS_DIR
    assert {p.name for p in folder.iterdir()} == set(files)
    for name, text in files.items():
        assert (folder / name).read_text(encoding="utf-8") == text, name
    rows = [line.split("\t") for line in files["index.tsv"].splitlines()]
    assert len(rows) == 20 and rows[0][0] == "kevin-ohara" and rows[1][0] == "nadia-petrov"
    assert all(len(r) == 5 for r in rows)
    assert next(r for r in rows if r[0] == "channel")[3] == "no"


def test_chat_zip_is_fresh_and_holds_only_what_colleagues_may_read() -> None:
    built = z.build(ROOT)
    assert (ROOT / z.OUTPUT).read_bytes() == built, "stale: run python -m harborline.tools.build_chat_zip"
    names = zipfile.ZipFile(io.BytesIO(built)).namelist()
    assert all(n.startswith("harborline-lab/") and "\\" not in n for n in names)
    inside = {n.split("/", 1)[1] for n in names}
    assert "START_HERE.html" in inside and "team/iris-review/references/chat.md" in inside
    assert any(n.startswith("tracker/dashboards/") for n in inside)
    assert "handbook/source-systems.html" in inside and "brand/specs/formats.json" in inside
    banned = ("answer_key/", "simulator/", "config/", "db/", "web/", "team/personas", "team/prompts", ".env")
    assert not [n for n in inside if n.startswith(banned)]
    assert "handbook/answer-key.html" not in inside and "handbook/team.html" not in inside


def test_any_ai_site_works() -> None:
    win = WINDOWS.read_text(encoding="ascii")
    mac = MAC.read_text(encoding="utf-8")
    for text in (win, mac):
        for address in (
            "https://chatgpt.com/",
            "https://claude.ai/new",
            "https://gemini.google.com/app",
            "https://copilot.microsoft.com/",
        ):
            assert address in text
    for text in (win, mac):
        assert "Another site" in text and "team-site" in text
    assert "ChatGPT" not in (ROOT / "team" / "prompts" / "iris-novak.txt").read_text(encoding="utf-8")
    assert "chatgpt.com" not in (ROOT / "team" / "prompts" / "kevin-ohara.txt").read_text(encoding="utf-8")


def test_known_issues_page_is_fresh_and_complete() -> None:
    assert (ROOT / k.OUTPUT).read_text(encoding="utf-8") == k.render(), (
        "stale: run python -m harborline.tools.build_known_issues"
    )
    text = k.render()
    for error in (
        "docker_engine",
        "HTTP 401",
        "No package found",
        "no configuration file provided",
        "SQLITE_ERROR",
        "dbo.custtable",
        "cross-database references",
        "pre-commit&#x27; is not recognized",
        "forbidden by its access permissions",
        "0x80370102",
        "toomanyrequests",
        "GH007",
    ):
        assert error in text, error
    for _, _, items in k.GROUPS:
        for where, who, error, why, action, source in items:
            assert where in {"Windows", "Mac", "Both"} and who in {k.SETUP, k.YOU}
            assert error and why and action and (not source or source.startswith("https://"))
    assert "known-issues.html" in (ROOT / "handbook" / "getting-started.html").read_text(encoding="utf-8")
    assert "known-issues.html" in (ROOT / "team" / "prompts" / "kevin-ohara.txt").read_text(encoding="utf-8")


def test_setup_can_never_wipe_finished_data() -> None:
    """A slow answer once read as "no data yet" and wiped a finished 12-hour build."""
    for text in (WINDOWS.read_text(encoding="ascii"), MAC.read_text(encoding="utf-8")):
        assert not re.search(r"""replace\W{1,6}(\$)?true""", text), "only a person pressing Build may replace data"
        assert len(re.findall(r"/api/sim/build\b", text.replace("POST /api/sim/build\"", ""))) == 1, "one place starts a build"
        assert "did not answer for 5 minutes. Nothing was wiped" in text
    win = WINDOWS.read_text(encoding="ascii")
    mac = MAC.read_text(encoding="utf-8")
    # Finished means the stamp in the database, nothing else.
    assert "if ($s.built_at) { return $true }" in win
    assert '[ -n "$built" ] && return 0' in mac
    # The app on this machine answering slowly is not a network problem.
    assert "'^(GET|POST|PUT) /api/'" in win and "own_app_failed" in mac
    # A running build is never restarted without asking.
    assert "Get-RunningBuild" in win and "running_build" in mac


def test_setup_asks_which_size_to_build_and_suggests_medium() -> None:
    win = WINDOWS.read_text(encoding="ascii")
    mac = MAC.read_text(encoding="utf-8")
    for text in (win, mac):
        for size in ("Small", "Medium", "Large"):
            assert re.search(rf"\d  {size} ", text), size
        assert "Type 1, 2 or 3" in text
    assert "Ask-Text 'Type 1, 2 or 3' '2'" in win and 'ask_text "Type 1, 2 or 3" "2"' in mac


def test_medium_has_everything_the_projects_use() -> None:
    rules = b.load_yaml(ROOT / "config" / "business_rules.yaml")
    planted = b.load_yaml(ROOT / "answer_key" / "planted_stories.yaml")
    assert all("medium" in w["presets"] for w in rules["warehouses"]), "tickets use every warehouse"
    months = rules["size_presets"]["medium"]["months"]
    clients = {c["code"] for c in rules["clients"]}
    for story in planted["stories"]:
        assert -int(story.get("month", 0)) < months, story["id"]
        assert not story.get("client") or story["client"] in clients, story["id"]
    assert rules["size_presets"]["medium"]["months"] >= 24, "a year-on-year comparison needs two years"


def test_nothing_in_the_tickets_requires_the_large_size() -> None:
    for path in sorted((ROOT / "tracker" / "tickets").glob("*.yaml")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if re.search(r"(?<!or )\bLarge (preset|dataset|build|backfill)\b|for Large\"", line):
                raise AssertionError(f"{path.name}: {line.strip()}")


def test_the_app_repairs_a_landing_folder_docker_recreated_as_root() -> None:
    """A real failure: landing was deleted, Docker recreated it owned by root, and the app could never write there."""
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    entry = (ROOT / "simulator" / "entrypoint.sh").read_bytes()
    docker = (ROOT / "simulator" / "Dockerfile").read_text(encoding="utf-8")
    assert 'user: "0:0"' in compose and "HL_UID: ${HL_UID:-10001}" in compose
    assert b"\r" not in entry and b"chown" in entry and b"exec $as_app" in entry and b"/data/landing" in entry
    assert "entrypoint.sh" in docker and "ENTRYPOINT" in docker and "s/\\r$//" in docker
    assert "landing folder" in WINDOWS.read_text(encoding="ascii") and "landing-folder" in MAC.read_text(encoding="utf-8")


def test_a_click_cannot_pause_the_windows_setup() -> None:
    text = WINDOWS.read_text(encoding="ascii")
    # Quick edit is the console setting that freezes output on a click: 0x40 under the extended flags, 0x80.
    assert "$quickEdit = 0x40" in text and "$extendedFlags = 0x80" in text
    guard_ends = text.index(". ([ScriptBlock]::Create($Script:ClickGuard))")
    assert text.index("Set-ClickSelects $false", guard_ends) < text.index("function Write-Log")
    assert re.search(r"function Invoke-Tool \{.*?Set-ClickSelects \$false.*?& \$Exe", text, re.S)
    # The administrator window is a console of its own.
    assert "$($Script:ClickGuard)`nSet-ClickSelects `$false" in text
    # Clicks select again only while setup waits, so paste and copy still work. Read-Host is only ever behind this.
    assert re.search(r"Set-ClickSelects \$true\s+\$answer = Read-Host \$Label\s+Set-ClickSelects \$false", text)
    assert text.count("Read-Host") == 1
    assert re.search(r"Set-ClickSelects \$true\s+\$p = Start-Process.*?\$p\.WaitForExit\(\)\s+Set-ClickSelects \$false", text, re.S)


def test_the_zip_is_stressed_wherever_a_chat_starts() -> None:
    for wizard in (WINDOWS.read_text(encoding="ascii"), MAC.read_text(encoding="ascii")):
        assert "IMPORTANT: send harborline-chat.zip WITH the prompt" in wizard
        assert "all in ONE message" in wizard and "wait until it finishes uploading" in wizard
        assert "tell me if you can open it" in wizard  # the first reply shows whether the zip arrived
        assert "(attach it with every prompt)" in wizard
    assert "Always send the zip with the prompt" in (ROOT / "handbook" / "team.html").read_text(encoding="utf-8")
    team_page = (ROOT / "web" / "src" / "features" / "team" / "Team.tsx").read_text(encoding="utf-8")
    assert "Send the zip with the prompt" in team_page and "person.needsZip" in team_page
    assert "it's missing, ask" in (ROOT / "team" / "prompts" / "luis-moreno.txt").read_text(encoding="utf-8")


def test_no_planted_problem_blames_a_real_company() -> None:
    planted = yaml.safe_load((ROOT / "answer_key" / "planted_stories.yaml").read_text(encoding="utf-8"))
    carriers = {story["scac"] for story in planted["stories"] if story.get("scac")}
    assert carriers == {"VRPX", "MRLF"}  # Velocity Regional Parcel and Marlowe Freight Lines, both invented


def test_the_project_is_ready_to_publish_on_github() -> None:
    for name in ("LICENSE", "README.md", "CONTRIBUTING.md", "SECURITY.md", ".github/ISSUE_TEMPLATE/setup-failed.yml",
                 ".github/workflows/ci.yml", ".github/workflows/demo.yml"):
        assert (ROOT / name).is_file(), name
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for media in re.findall(r'src="(docs/media/[^"]+)"', readme):
        assert (ROOT / media).is_file(), media
    assert "SETUP-WINDOWS.cmd" in readme and "SETUP-MAC.command" in readme
    # Line endings have to survive a clone or GitHub's Download ZIP, or cmd.exe can't read the setup file.
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "*.cmd text eol=crlf" in attributes and "*.command text eol=lf" in attributes and "*.zip binary" in attributes


def test_both_wizards_handle_the_researched_problems() -> None:
    win = WINDOWS.read_text(encoding="ascii")
    mac = MAC.read_text(encoding="utf-8")
    for text in (win, mac):
        for needle in (
            "ms-azuretools.vscode-containers",
            "commit.gpgsign=false",
            "GH007",
            "unrelated histories",
            "--install-hooks",
            "toomanyrequests",
            "no space left on device",
            "docker-credential-desktop",
            "Hi ",
            "stopped answering for 10 minutes",
        ):
            assert needle in text, needle
    assert "--web-download" in win and "TcpListener" in win and "Stop-WslIfOnlyDocker" in win
    assert "wsl-restart.done" in win and "LanguageMode" in win
    assert "nc -z -G 2" in mac and "softwareupdate -i" in mac and "BASH_VERSION" in mac
    assert "ms-azuretools.vscode-docker" not in win + mac
    # A terminal only sees what was installed before it opened: retry once fresh, and say so at the end.
    assert "Restart-InFreshWindow" in win and 'if "%HL_RC%"=="75" exit /b 0' in win
    assert "restart_fresh_shell" in mac and "HL_SETUP_RESTARTED" in mac and "NO_FRESH_SHELL" in mac
    assert "-Optional -NoFreshWindow" in win
    for text in (win, mac):
        assert "Old windows cannot see what was just installed" in text


# ---------------------------------------------------------------------------------------------- updates
def _before_first_step(text: str, first_step: str) -> str:
    return text[: text.index(first_step)]


def test_the_update_check_runs_before_step_one_and_cannot_block() -> None:
    win = WINDOWS.read_text(encoding="ascii")
    mac = MAC.read_text(encoding="ascii")
    head = _before_first_step(win, "Step 'Check this computer'")
    assert head.index("Stop-IfOlderThanInstalled\nStart-UpdateCheck") > head.index("hook for the tests")
    assert head.index("Start-UpdateCheck\n") < head.index("$Script:Machine = Get-MachineInfo")
    head = _before_first_step(mac, 'step "Check this computer"')
    assert "stop_if_older_than_installed\nstart_update_check\n" in head[head.index("hook for the tests") :]
    # A hard limit on the first read. On Windows the task is waited on, because a web request's own timeout leaves out DNS.
    assert "[int]$TimeoutSec = 10" in win and ".Wait($limit)" in win
    assert "--max-time 10" in mac
    # A failure is one line in the log: nothing on screen, nothing that stops.
    assert 'Write-Log "Update check skipped: $(Get-ErrorText $_)"' in win
    assert 'log "Update check skipped: could not read the latest version."' in mac
    assert "2>>\"$LOG_FILE\"" in mac
    for text in (win, mac):
        assert "is available. Update now? Your data, settings and progress are kept." in text
        assert "raw.githubusercontent.com/" in text and "/main/setup/VERSION" in text
        assert "/archive/refs/heads/main.zip" in text and "setup/UPDATE_REPO" in text.replace("\\", "/")
        assert "You have the latest version" in text
        assert "The update did not work, so setup is carrying on with version" in text
    # The app's proxy switch-off must not cut the update check off from a company proxy.
    assert win.index("GetSystemWebProxy") < win.index("DefaultWebProxy = New-Object System.Net.WebProxy")
    assert "$handler.Proxy = $Script:SystemProxy" in win
    assert "use_system_proxy; exec curl" in mac


def test_an_update_cannot_loop_and_windows_leaves_before_anything_is_copied() -> None:
    win = WINDOWS.read_text(encoding="ascii")
    mac = MAC.read_text(encoding="ascii")
    assert "if ($env:HL_SETUP_UPDATED)" in win and 'if [ -n "$HL_SETUP_UPDATED" ]' in mac
    assert 'export HL_SETUP_UPDATED\n  exec /bin/bash "$top/SETUP-MAC.command"' in mac
    assert '"$got" != "$version"' in mac and "if ($got -ne $Version)" in win
    # Windows: a fresh cmd.exe with the marker on its command line, then 75. Explorer would lose the marker.
    start = win[win.index("function Start-SetupInNewWindow") : win.index("function Stop-IfOlderThanInstalled")]
    assert "'cmd.exe'" in start and "explorer" not in start.split("Start-Process")[1]
    assert "HL_SETUP_UPDATED=" in start and start.rstrip().endswith("exit 75\n}")
    update = win[win.index("function Start-Update(") : win.index("function Start-UpdateCheck")]
    assert "Unblock-File" in update and "Copy-Item" not in update and "Copy-Package" not in update
    assert 'if "%HL_RC%"=="75" exit /b 0' in win
    # Mac: unzip must not eat the answers on stdin, and the new file stays runnable.
    assert 'unzip -q -o "$zip" -d "$UPDATE_DIR" </dev/null' in mac and 'chmod +x "$top/SETUP-MAC.command"' in mac


def test_an_update_never_sends_replace_or_starts_a_build() -> None:
    win = WINDOWS.read_text(encoding="ascii")
    mac = MAC.read_text(encoding="ascii")
    for text, start, end in (
        (win, "# ---------------------------------------------------------------------------------------------- updates", "hook for the tests"),
        (mac, "# ---------------------------------------------------------------------------------------------- updates", "hook for the tests"),
    ):
        part = text[text.index(start) : text.index(end)]
        assert "/api/sim/build" not in part and "replace" not in part.replace("-replace", "").replace(".Replace", "")
        assert "Start-DataBuild" not in part and "start_data_build" not in part
        assert "Updated to" in part and "/api/sim/status" in part


def test_old_files_never_overwrite_new_ones() -> None:
    win = WINDOWS.read_text(encoding="ascii")
    mac = MAC.read_text(encoding="ascii")
    assert "Test-NewerVersion $installed $mine" in win and 'version_newer "$installed" "$mine"' in mac
    assert "already installed, so setup will not copy old files over it" in win
    assert "already installed, so setup will not copy old files over it" in mac
    # VERSION goes last, so a copy that stopped halfway is done again next time.
    assert "--exclude='/setup/VERSION'" in mac
    assert mac.index("remove_stale_files \"$files\"") < mac.index('cp "$SOURCE_DIR/setup/VERSION" "$LAB_DIR/setup/VERSION"')
    assert "if ($relative -eq 'setup/VERSION') { continue }" in win


def test_removing_old_files_only_touches_what_setup_put_there() -> None:
    win = WINDOWS.read_text(encoding="ascii")
    mac = MAC.read_text(encoding="ascii")
    assert "installed-files.txt" in win and "installed-files.txt" in mac
    for text in (win, mac):
        assert "landing" in text and "answer_key" in text and "anomalies" in text
    assert "$Relative -eq '.env'" in win and "$Relative -eq 'answer_key/anomalies.csv'" in win
    assert "--exclude='/.env'" in mac and "--exclude='/landing/'" in mac and "--exclude='/answer_key/anomalies.csv'" in mac
    # A file renamed only by case is the same file on these disks.
    assert "OrdinalIgnoreCase" in win and "tolower($0)" in mac


def _bash_function(name: str) -> str:
    text = MAC.read_text(encoding="ascii")
    match = re.search(rf"(?ms)^{name}\(\) \{{.*?^\}}$|^{name}\(\) \{{[^\n]*\}}$", text)
    assert match, name
    return match.group(0)


@pytest.mark.parametrize(("a", "b", "newer"), [
    ("2026.11.10", "2026.11.9", True),
    ("2026.11.9", "2026.11.10", False),
    ("2026.11.9", "2026.11.9", False),
    ("2026.12.1", "2026.11.99", True),
    ("2027.1", "2026.12.31", True),
    ("2026.11.9.1", "2026.11.9", True),
    ("2026.11.09", "2026.11.9", False),
    ("<html>", "2026.11.9", False),
    ("", "2026.11.9", False),
    ("2026.11.10", "", False),
])
def test_versions_compare_as_numbers(a: str, b: str, newer: bool) -> None:
    script = _bash_function("version_ok") + "\n" + _bash_function("version_newer") + f'\nversion_newer "{a}" "{b}"'
    assert (subprocess.run(["/bin/bash", "-c", script], check=False).returncode == 0) is newer
    pwsh = shutil.which("pwsh")
    if pwsh:
        win = WINDOWS.read_text(encoding="ascii")
        code = win[win.index("function Test-VersionText") : win.index("function Get-UpdateRepo")]
        out = subprocess.run([pwsh, "-NoProfile", "-Command", code + f"; Test-NewerVersion '{a}' '{b}'"], check=True,
                             capture_output=True, text=True).stdout.strip()
        assert out == str(newer)


def test_update_repo_ships_and_the_docs_say_how_updating_works() -> None:
    assert (ROOT / "setup" / "UPDATE_REPO").read_text(encoding="utf-8").strip() == "FaresElsayed/harborline-lab"
    for name in (".dockerignore", ".gitignore"):
        assert "UPDATE_REPO" not in (ROOT / name).read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert readme.index("## Updating") < readme.index("## What it needs")
    assert "C:\\dev\\harborline-lab\\SETUP-WINDOWS.cmd" in readme and "~/dev/harborline-lab/SETUP-MAC.command" in readme
    assert "releases a new version to everyone" in (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "checks\nfor a new version" in (ROOT / "handbook" / "getting-started.html").read_text(encoding="utf-8")
    page = k.render()
    for text in ("The update did not work", "raw.githubusercontent.com", "both gone"):
        assert text in page, text


def _version_client(handbook_dir: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("HL_HANDBOOK_DIR", str(handbook_dir))
    reset_settings()
    app = FastAPI()
    app.include_router(control.router)
    return TestClient(app)


def test_api_version_reads_the_setup_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "setup").mkdir()
    (tmp_path / "handbook").mkdir()
    (tmp_path / "setup" / "VERSION").write_text("2026.11.10\n", encoding="utf-8")
    (tmp_path / "setup" / "UPDATE_REPO").write_text("someone/fork\n", encoding="utf-8")
    try:
        answer = _version_client(tmp_path / "handbook", monkeypatch).get("/api/version")
        assert answer.status_code == 200
        assert answer.json() == {"version": "2026.11.10", "update_repo": "someone/fork"}
    finally:
        monkeypatch.undo()
        reset_settings()


def test_api_version_is_empty_when_the_files_are_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        answer = _version_client(tmp_path / "nowhere", monkeypatch).get("/api/version")
        assert answer.status_code == 200
        assert answer.json() == {"version": "", "update_repo": ""}
    finally:
        monkeypatch.undo()
        reset_settings()


# ---------------------------------------------------------------------------------------------- keys
KEY_NAMES = ("POSTGRES_PASSWORD", "HL_APP_PASSWORD", "HL_ETL_PASSWORD", "HL_REVIEWER_PASSWORD",
             "SF_CLIENT_ID", "SF_CLIENT_SECRET", "CARRIER_API_KEY")
# The sample values that used to ship. Built from parts, so this file stays clean by the same test.
OLD_KEYS = tuple(a + b for a, b in (
    ("harborline", "-admin"), ("harborline", "-app"), ("etl-reader", "-2026"), ("uat-review", "-2026"),
    ("hl-carrier-live", "-5e2a9d"), ("hl-sf-secret", "-7c41"), ("3MVG9harborline", ".etl.client"),
))
NOT_THE_PACKAGE = {".venv", "node_modules", "dist", "__pycache__", ".pytest_cache", ".ruff_cache", "landing", ".claude", ".git"}


def _hits(data: bytes) -> list[str]:
    return [key for key in OLD_KEYS if key.encode() in data]


def test_no_key_is_in_the_package() -> None:
    offenders = []
    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT)
        # A person's own .env is theirs, and Git ignores it.
        if not path.is_file() or NOT_THE_PACKAGE & set(rel.parts) or rel.as_posix() == ".env" or ".egg-info" in rel.as_posix():
            continue
        data = path.read_bytes()
        if hits := _hits(data):
            offenders.append(f"{rel}: {hits}")
        if path.suffix == ".zip":
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                offenders += [f"{rel}!{name}" for name in archive.namelist() if _hits(archive.read(name))]
    assert not offenders


def test_no_secret_has_a_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in KEY_NAMES:
        monkeypatch.delenv(name, raising=False)
    s = load_settings()
    for value in (s.pg_superpassword, s.app_password, s.etl_password, s.reviewer_password,
                  s.sf_client_id, s.sf_client_secret, s.carrier_api_key):
        assert value == ""
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in KEY_NAMES:
        assert f"${{{name}:?" in compose, name
        assert re.search(rf"(?m)^{name}=$", example), name
    assert ":-" not in re.sub(r"\$\{HL_(BIND_ADDR|PG_PORT|APP_PORT|UID|GID):-[^}]*\}", "", compose)
    assert "setup fills them in" in example
    assert not (ROOT / ".gitleaks.toml").exists()
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "zricethezav/gitleaks:v8.28.0 detect --source /repo --no-git" in ci and "openssl rand -hex 16" in ci
    assert "generated on your machine during setup" in (ROOT / "SECURITY.md").read_text(encoding="utf-8")


def test_every_route_generates_the_same_keys_and_only_fills_blanks() -> None:
    win = WINDOWS.read_text(encoding="ascii")
    mac = MAC.read_text(encoding="ascii")
    start = (ROOT / "start.sh").read_text(encoding="utf-8")
    fill = (ROOT / "setup" / "fill-env.ps1").read_bytes()
    assert fill.isascii() and fill.count(b"\n") == fill.count(b"\r\n")
    fill_text = fill.decode("ascii")
    for text in (win, mac, start, fill_text):
        for name in KEY_NAMES:
            assert name in text, name
    for text in (win, fill_text):
        assert "[Security.Cryptography.RandomNumberGenerator]::Create()" in text and "New-Object byte[] 24" in text
    for text in (mac, start):
        assert "openssl rand -hex 24" in text and "/dev/urandom" in text
    assert "if (Get-EnvValue $key) { continue }" in win
    assert '[ -z "$(env_get "$key" "")" ] || continue' in mac
    assert "fill-env.ps1" in (ROOT / "start.bat").read_text(encoding="utf-8")
    # A step that reads a key has no fallback left.
    assert "$pw = Get-EnvValue 'HL_ETL_PASSWORD'\n" in win and 'pw="$(env_get HL_ETL_PASSWORD "")"' in mac
    # Losing the password with data still there stops and explains. Setup itself never removes the data.
    for text in (win, mac):
        assert "both gone, but your simulator data is still here" in text and "Keep your data" in text
        assert "down -v" not in text and "down --volumes" not in text
    assert "@('volume', 'rm'" not in win and "run docker volume rm" not in mac


def _start_sh_sandbox(tmp_path: Path, name: str) -> tuple[Path, dict[str, str]]:
    """A folder with start.sh and .env.example, with fake docker, curl and open on the PATH."""
    lab = tmp_path / name
    lab.mkdir()
    for file in ("start.sh", ".env.example"):
        shutil.copy(ROOT / file, lab / file)
    fake = tmp_path / "bin"
    if not fake.exists():
        fake.mkdir()
        docker = '#!/bin/bash\ncase "$1 $2" in "volume ls") [ -f "$HL_FAKE_VOLUME" ] && echo harborline-lab_pgdata ;; esac\nexit 0\n'
        for tool, body in (("docker", docker), ("curl", "#!/bin/bash\nexit 0\n"), ("open", "#!/bin/bash\nexit 0\n"), ("xdg-open", "#!/bin/bash\nexit 0\n")):
            (fake / tool).write_text(body, encoding="utf-8")
            (fake / tool).chmod(0o755)
    env = {"PATH": f"{fake}:/usr/bin:/bin", "HOME": str(tmp_path), "HL_STATE_DIR": str(tmp_path / f"state-{name}"),
           "HL_FAKE_VOLUME": str(tmp_path / "volume")}
    return lab, env


def _run_start(lab: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["/bin/bash", "start.sh"], cwd=lab, env=env, check=False, capture_output=True, text=True)


def _keys(lab: Path) -> dict[str, str]:
    lines = (lab / ".env").read_text(encoding="utf-8").splitlines()
    return {k: v for k, _, v in (line.partition("=") for line in lines) if k in KEY_NAMES}


def test_start_sh_makes_different_keys_and_never_rerolls(tmp_path: Path) -> None:
    one, env_one = _start_sh_sandbox(tmp_path, "one")
    two, env_two = _start_sh_sandbox(tmp_path, "two")
    assert _run_start(one, env_one).returncode == 0
    assert _run_start(two, env_two).returncode == 0
    keys = _keys(one)
    assert set(keys) == set(KEY_NAMES)
    assert all(re.fullmatch(r"[0-9a-f]{32,}", value) for value in keys.values())
    assert len(set(keys.values())) == len(KEY_NAMES)
    assert not set(keys.values()) & set(_keys(two).values())
    # A rerun changes nothing.
    before = (one / ".env").read_bytes()
    assert _run_start(one, env_one).returncode == 0
    assert (one / ".env").read_bytes() == before
    # One blank key: only that one is filled in, and values that were there stay, old samples included.
    env_file = one / ".env"
    text = env_file.read_text(encoding="utf-8")
    text = re.sub(r"(?m)^CARRIER_API_KEY=.*$", "CARRIER_API_KEY=", text)
    text = re.sub(r"(?m)^HL_ETL_PASSWORD=.*$", "HL_ETL_PASSWORD=kept-as-it-was", text)
    env_file.write_text(text, encoding="utf-8")
    Path(env_one["HL_STATE_DIR"], "env.backup").unlink()
    assert _run_start(one, env_one).returncode == 0
    after = _keys(one)
    assert after["HL_ETL_PASSWORD"] == "kept-as-it-was"
    assert after["CARRIER_API_KEY"] not in ("", keys["CARRIER_API_KEY"])
    assert all(after[k] == keys[k] for k in KEY_NAMES if k not in ("HL_ETL_PASSWORD", "CARRIER_API_KEY"))


def test_start_sh_restores_a_lost_env_and_stops_rather_than_lock_someone_out(tmp_path: Path) -> None:
    lab, env = _start_sh_sandbox(tmp_path, "lab")
    assert _run_start(lab, env).returncode == 0
    keys = _keys(lab)
    (lab / ".env").unlink()
    assert _run_start(lab, env).returncode == 0
    assert _keys(lab) == keys
    # The file and its backup both gone with data in Docker: no new password, nothing deleted.
    (lab / ".env").unlink()
    Path(env["HL_STATE_DIR"], "env.backup").unlink()
    (tmp_path / "volume").touch()
    stopped = _run_start(lab, env)
    assert stopped.returncode == 1 and "both gone" in stopped.stderr
    assert not (lab / ".env").exists()
    # With no data there is nothing to lock out, so new keys are safe.
    (tmp_path / "volume").unlink()
    assert _run_start(lab, env).returncode == 0
    assert set(_keys(lab)) == set(KEY_NAMES) and _keys(lab) != keys
