"""Render handbook/known-issues.html: every error we know of, per setup step, with who deals with it and how.

Researched from vendor docs and issue trackers (November 2026). The setup files fix what they can by themselves; this
page, and the advice printed when setup stops, cover the rest. Run from the package root:

    python -m harborline.tools.build_known_issues
"""

from __future__ import annotations

from html import escape
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[3]
OUTPUT = Path("handbook") / "known-issues.html"

SETUP = "setup"  # the setup file fixes this by itself
YOU = "you"  # a person has to act; setup explains what to do

# (anchor, title, [(where, who, error text, why, what to do, source)])
GROUPS: list[tuple[str, str, list[tuple[str, str, str, str, str, str]]]] = [
    ("launching", "Starting the setup file", [
        ("Windows", SETUP, "The window title starts with Select and the output has stopped", "A click started a text selection, which freezes a classic console window until a key is pressed.", "Setup turns this off while it works, and turns it back on only when it is waiting for your answer. If you still see it, press Esc.", "https://learn.microsoft.com/en-us/windows/console/setconsolemode"),
        ("Both", YOU, "is not recognized as the name of a cmdlet, command not found, right after setup", "A terminal only sees programs installed before it opened. Windows you already had open, including VS Code's terminal, can't see the new ones.", "Close them and open a new one. During setup this is handled for you: it refreshes its own window, and if a step fails right after an install it carries on once in a fresh one.", "https://learn.microsoft.com/en-us/windows/package-manager/winget/install"),
        ("Windows", YOU, "Windows protected your PC", "SmartScreen doesn't know this file.", "Press More info, then Run anyway.", "https://learn.microsoft.com/en-us/windows/security/operating-system-security/virus-and-threat-protection/microsoft-defender-smartscreen/"),
        ("Windows", YOU, "Smart App Control blocked an app", "Smart App Control blocks downloaded .cmd files and has no override.", "In PowerShell, run Unblock-File on the zip in your Downloads folder, then extract it again.", "https://textslashplain.com/2026/04/28/smart-app-control/"),
        ("Windows", YOU, "Method invocation is supported only on core types", "A company policy (AppLocker or WDAC) restricts PowerShell. Setup detects this and says so.", "Whoever manages the computer has to allow the file.", "https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_language_modes"),
        ("Windows", YOU, "ScriptContainedMaliciousContent, blocked by your antivirus", "Antivirus blocked the script.", "Review it under Windows Security, Protection history.", "https://learn.microsoft.com/defender-endpoint/mde-demonstration-amsi"),
        ("Windows", YOU, "This file is being run from inside the zip", "Windows lets you open a zip like a folder, but the rest of the package isn't really there.", "Right-click the zip, choose Extract All, and run the file from the extracted folder.", ""),
        ("Windows", SETUP, "Setup doesn't restart by itself after a reboot, from a folder with brackets in its name", "Windows dropped the quotes around the path, and it only tries once.", "Fixed in how setup registers itself. If it still doesn't start, run the file again.", "https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/cmd"),
        ("Mac", YOU, "Apple could not verify, unidentified developer, appropriate access privileges", "Double-clicking a downloaded file, or a zip tool dropped its run permission.", "Open Terminal, type bash and a space, drag the file in, press Return. This avoids the check.", "https://developer.apple.com/news/?id=saqachfa"),
        ("Mac", YOU, "Operation not permitted", "macOS denied Terminal access to Downloads, Desktop or Documents.", "System Settings, Privacy & Security, Files & Folders, Terminal: allow it. Or move the folder to your home folder.", "https://support.apple.com/guide/mac-help/control-access-to-files-and-folders-on-mac-mchld5a35146/mac"),
        ("Mac", YOU, "no such file or directory: bash/, is a directory, cannot execute binary file", "A typing or dragging slip: no space after bash, or the folder or zip was dragged in.", "Type bash, a space, then drag in SETUP-MAC.command itself.", ""),
        ("Mac", SETUP, "Started with zsh or sh, or from a Rosetta Terminal", "Parts of the script need bash, and Homebrew refuses Rosetta on Apple Silicon.", "Setup restarts itself the right way.", ""),
        ("Mac", YOU, "Resource deadlock avoided", "The folder is in iCloud and some files aren't on this Mac yet.", "Extract the zip outside iCloud, for example in your home folder.", "https://awesome.ecosyste.ms/projects/github.com%2Ficanhasjonas%2Ficloud-tools"),
    ]),
    ("computer", "Check this computer", [
        ("Windows", YOU, "This Windows is too old", "Docker Desktop needs Windows 10 22H2 (build 19045) or Windows 11 23H2 (build 22631). Builds 22000 to 22630 fall between them, and Server isn't supported.", "Run Windows Update, then run the file again.", "https://docs.docker.com/desktop/setup/install/windows-install/"),
        ("Windows", YOU, "Hardware virtualization is off", "VT-x or SVM is off in the BIOS. Windows sometimes misreports this, so setup asks you to check Task Manager first.", "Task Manager, Performance, CPU. If it says Disabled, enable it in the firmware settings.", "https://learn.microsoft.com/en-us/windows/wsl/troubleshooting"),
        ("Windows", SETUP, "Could not read the computer's details", "A damaged WMI used to read as a too-old Windows.", "Setup reads the build from the registry and carries on.", "https://learn.microsoft.com/en-us/windows/win32/wmisdk/winmgmt"),
        ("Windows", YOU, "Windows on Arm", "Docker Desktop is Early Access there, and some extensions have no Arm build.", "Expect rough edges. Ask Kevin if a step fails.", "https://docs.docker.com/desktop/setup/install/windows-install/"),
        ("Mac", YOU, "Docker Desktop needs macOS 14 or newer", "Older versions aren't supported by the current Docker Desktop.", "Update macOS.", "https://docs.docker.com/desktop/setup/install/mac-install/"),
        ("Mac", YOU, "You are connected over SSH", "Setup opens apps and uses the clipboard.", "Run it at the Mac itself.", ""),
    ]),
    ("winget", "Windows package manager (winget)", [
        ("Windows", SETUP, "winget is not recognized, Program 'winget.exe' failed to run", "App Installer is missing, broken or too old.", "Setup re-registers it, then repairs it from the PowerShell Gallery, then opens the Store page.", "https://github.com/microsoft/winget-cli/tree/master/doc/troubleshooting"),
        ("Windows", SETUP, "Failed in attempting to update the source, Failed when opening source, Failed when searching source, 0x8a15005e", "winget's package list is out of date or damaged.", "Setup refreshes it, then resets it as administrator.", "https://learn.microsoft.com/en-us/windows/package-manager/winget/source"),
        ("Windows", SETUP, "No package found matching input criteria", "A package id changed or the list is stale.", "Setup refreshes the list and retries. By hand: winget search <name>, and use the Id it shows.", "https://learn.microsoft.com/en-us/windows/package-manager/winget/"),
        ("Windows", SETUP, "Found an existing package already installed, No applicable upgrade found", "Already there. Not an error, whatever the Windows language.", "Setup reads winget's exit code and moves on.", "https://github.com/microsoft/winget-cli/blob/master/doc/windows/package-manager/winget/returnCodes.md"),
        ("Windows", SETUP, "Installer failed with exit code", "Some installers fail for a normal user, Docker Desktop among them.", "Setup tries again as administrator. Click Yes.", "https://github.com/microsoft/winget-pkgs/issues/93495"),
        ("Windows", YOU, "You cancelled the installation, 800704c7", "The Windows permission prompt was refused.", "Run the file again and press Yes.", "https://github.com/microsoft/winget-cli/issues/6173"),
        ("Windows", SETUP, "0x801901a0, 0x80d03002", "Windows' Delivery Optimization downloader failed.", "Setup switches winget to its plain downloader.", "https://github.com/microsoft/winget-cli/tree/master/doc/troubleshooting"),
        ("Windows", YOU, "0x80072efd", "No route to the internet, usually a proxy or allowlist.", "As administrator: winget settings --enable ProxyCommandLineOptions. Or try another network.", "https://github.com/microsoft/winget-cli/tree/master/doc/troubleshooting"),
        ("Windows", YOU, "Installer hash does not match, 0x8a150011", "The vendor released a new installer before winget caught up.", "Setup refreshes and retries once. If it persists, try tomorrow or use the vendor's installer. Never skip the check.", "https://github.com/microsoft/winget-pkgs/issues/247897"),
        ("Windows", YOU, "disabled by Group Policy, Organization policies are preventing", "Company policy.", "Ask IT, or use vendor installers, then run the file again. winget --info lists the policies.", "https://techcommunity.microsoft.com/blog/windows-itpro-blog/manage-windows-package-manager-with-group-policy/2346322"),
    ]),
    ("terminal", "Terminal and PowerShell 7", [
        ("Windows", SETUP, "depends on a framework that could not be found, 0x80073CF3", "Windows 10 lacks Microsoft.UI.Xaml.", "Setup installs it and retries.", "https://github.com/microsoft/terminal/issues/18033"),
        ("Windows", YOU, "Another installation is already in progress, exit code 1618", "Another installer is running.", "Finish it or restart, then run the file again. Nothing else depends on this step.", "https://github.com/microsoft/winget-cli/blob/master/src/AppInstallerCLIPackage/Shared/Strings/en-us/winget.resw"),
    ]),
    ("wsl", "WSL2", [
        ("Windows", SETUP, "WSL2 is unable to start since virtualization is not enabled, Run wsl.exe --install", "Normal straight after installing: WSL needs one restart.", "Setup restarts once and carries on. If WSL is still broken after that, it stops with a report instead of restarting forever.", "https://github.com/microsoft/WSL/blob/master/src/windows/common/WslClient.cpp"),
        ("Windows", YOU, "Usage: wsl.exe", "Windows 10 lacks the update that adds these commands (KB5020030).", "Run Windows Update, restart, then run the file again.", "https://devblogs.microsoft.com/commandline/the-windows-subsystem-for-linux-in-the-microsoft-store-is-now-generally-available-on-windows-10-and-11/"),
        ("Windows", SETUP, "kernel file is not found, WSL 2 requires an update, 0x800701bc, Error: 0x1bc", "The WSL kernel is missing or old.", "Setup runs wsl --update.", "https://learn.microsoft.com/en-ca/answers/questions/657991/wsl2-not-working-out-of-the-box-on-windows-11"),
        ("Windows", SETUP, "Wsl/InstallDistro/0x80072f8f, Update failed (exit code: 1603), Wsl/CallMsi/E_ABORT, wsl update failed", "The Microsoft Store route is blocked or broken.", "Setup retries with wsl --update --web-download.", "https://github.com/microsoft/WSL/issues/10764"),
        ("Windows", YOU, "REGDB_E_CLASSNOTREG, Class not registered", "A broken WSL install that the update can't repair.", "Install the latest MSI from Microsoft's WSL releases on GitHub, then run the file again.", "https://forums.docker.com/t/docker-desktop-wsl-update-failed-docker-desktop-4-37-1-178610-and-windows-11-pro-version-24h2/146330"),
        ("Windows", YOU, "0x80370102, HCS_E_HYPERV_NOT_INSTALLED", "The hypervisor is off, usually turned off by an emulator or game installer, or the BIOS setting.", "Setup turns the Windows hypervisor on and restarts once. Otherwise: enable it in the BIOS, turn on nested virtualization inside a VM, update VMware to 15.5.5+ or VirtualBox to 6+.", "https://learn.microsoft.com/en-us/windows/wsl/troubleshooting"),
        ("Windows", SETUP, "0x80070422", "A cleaner app disabled the WSL service.", "Setup turns it back on as administrator.", "https://community.ccleaner.com/t/ccleaner-disabled-wslservice-breaking-wsl-completely-error-0x80070422/163531"),
        ("Windows", YOU, "0x80370114", "The virtual machine service is blocked.", "Setup starts it. If it persists: Windows Security, App & browser control, Exploit protection, Program settings, vmcompute.exe, Control Flow Guard: untick Override, restart.", "https://windowsreport.com/0x80370114/"),
        ("Windows", SETUP, "0x8007019e", "The WSL feature is off.", "Setup turns it on.", "https://learn.microsoft.com/en-us/windows/wsl/troubleshooting"),
        ("Windows", YOU, "0x80070003", "Windows is set to install apps on another drive.", "Settings, System, Storage, Advanced storage settings, Where new content is saved: set new apps to C:.", "https://learn.microsoft.com/en-us/windows/wsl/troubleshooting"),
        ("Windows", SETUP, "0x8000ffff, Catastrophic failure, E_UNEXPECTED, 0x80004005", "A generic WSL failure.", "Setup updates and restarts WSL. If it persists, as administrator: sfc /scannow, then DISM /Online /Cleanup-Image /RestoreHealth, then restart.", "https://learn.microsoft.com/en-us/windows/wsl/troubleshooting"),
        ("Windows", YOU, "0x800704ec, blocked by group policy", "Company policy blocks WSL.", "Ask IT.", "https://learn.microsoft.com/en-us/windows/wsl/faq"),
        ("Windows", YOU, "Invalid memory string, Unknown key in .wslconfig", "A .wslconfig that was already there has a typo, so its limit is silently ignored. Setup never overwrites yours.", "Fix the file in your user folder, then run wsl --shutdown.", "https://learn.microsoft.com/en-us/windows/wsl/wsl-config"),
        ("Windows", SETUP, "Other Linux distributions are running, so setup did not restart WSL", "wsl --shutdown stops every distribution, so setup won't do it to your other work.", "Restart the computer yourself if Docker stays stuck.", "https://learn.microsoft.com/en-us/windows/wsl/basic-commands"),
    ]),
    ("docker", "Docker Desktop and its engine", [
        ("Both", SETUP, "failed to connect to the docker API, docker_engine, dockerDesktopLinuxEngine, Cannot connect to the Docker daemon", "Docker Desktop isn't running or is still starting. Installing it doesn't start it.", "Setup starts it and waits. The first time, accept the agreement and skip the sign-in.", "https://docs.docker.com/desktop/troubleshoot-and-support/troubleshoot/topics/"),
        ("Both", SETUP, "Engine reachable through the wrong context, Colima or OrbStack socket", "The command line points at another engine, or a stale context.", "Setup switches to desktop-linux.", "https://golang.testcontainers.org/system_requirements/using_colima/"),
        ("Both", SETUP, "DOCKER_HOST environment variable overrides, 192.168.99.100", "Left over from old Docker tools.", "Setup ignores these for its run. Remove them from your system settings if plain docker commands fail.", "https://raw.githubusercontent.com/docker/cli/master/cli/command/context/use.go"),
        ("Windows", SETUP, "WSL distro terminated abruptly, stuck on Starting the Docker Engine, Unexpected WSL error", "WSL stopped under Docker Desktop.", "Setup stops Docker Desktop, updates WSL, restarts it if nothing else is using it, and starts Docker again.", "https://forums.docker.com/t/docker-desktop-wsl-integration-fails-to-restart-when-unexpectedly-stopped/150572"),
        ("Windows", SETUP, "Docker Desktop - Access Denied, not in docker-users", "Your account isn't in the docker-users group, or the sign-in is older than the install.", "Setup adds you and asks for one restart, as a last resort.", "https://docs.docker.com/desktop/setup/install/windows-permission-requirements/"),
        ("Windows", SETUP, "no matching manifest for windows/amd64", "Docker is in Windows-containers mode.", "Setup switches it to Linux containers.", "https://docs.docker.com/desktop/troubleshoot-and-support/troubleshoot/topics/"),
        ("Windows", YOU, "Docker Desktop fails with the Server service stopped", "Docker needs the LanmanServer service on Automatic.", "As administrator: Set-Service LanmanServer -StartupType Automatic; Start-Service LanmanServer", "https://docs.docker.com/desktop/setup/install/windows-install/"),
        ("Both", YOU, "Sign in enforced by your administrators", "Your organisation requires a Docker sign-in.", "Sign in with your work account, or ask IT.", "https://docs.docker.com/enterprise/security/enforce-sign-in/"),
        ("Mac", SETUP, "It seems there is already a Binary at, It seems there is already an App at", "Left-over files, or an app installed by hand.", "Setup adopts the app, or lets Homebrew replace the files.", "https://github.com/Homebrew/homebrew-cask/issues/146078"),
        ("Mac", YOU, "Docker.app is damaged", "A known certificate problem on some versions.", "brew update, then brew reinstall --cask docker-desktop", "https://docs.docker.com/desktop/cert-revoke-solution/"),
    ]),
    ("git", "Git and your name on commits", [
        ("Both", SETUP, "empty ident name, Author identity unknown", "Name or email is missing or blank.", "Setup asks you for them.", "https://github.com/gitui-org/gitui/issues/79"),
        ("Both", SETUP, "detected dubious ownership", "The folder was created by an administrator window.", "Setup marks that one folder as safe, never everything.", "https://lore.kernel.org/git/fbfaff2ec21d76229eb4d86f6bfe4eab4de20c96.1659965270.git.gitgitgadget@gmail.com/T/"),
        ("Both", SETUP, "gpg failed to sign the data, failed to write commit object", "A global setting signs every commit, without a usable key.", "Setup skips signing for its one skeleton commit and leaves your setting alone.", "https://gist.github.com/paolocarrasco/18ca8fe6e63490ae1be23e84a7039374"),
        ("Both", YOU, "resolve proxy, failed to connect", "A stale proxy in Git's settings.", "See it with: git config --show-origin --get-regexp \"https?\\.proxy\". Remove it with: git config --global --unset http.proxy", "https://support.atlassian.com/bitbucket-data-center/kb/cant-clone-or-pull-due-to-a-git-outbound-proxy/"),
    ]),
    ("github", "GitHub sign-in and SSH key", [
        ("Both", SETUP, "HTTP 401: Bad credentials", "Someone chose Paste an authentication token. Setup always uses the browser route.", "Setup signs in again through the browser.", "https://cli.github.com/manual/gh_auth_login"),
        ("Both", SETUP, "failed to authenticate via web browser, slow_down, expired_token", "The one-time code took too long, or GitHub rate-limited the check.", "Setup tries once more. Enter the new code.", "https://github.com/cli/cli/issues/9370"),
        ("Both", SETUP, "unknown flag: --skip-ssh-key", "An old GitHub CLI.", "Setup updates it.", "https://github.com/mikekelly/simple-software-factory/issues/670"),
        ("Both", SETUP, "gh auth status fails although you are signed in", "An old stale account on the same host.", "Setup tests a real API call instead.", "https://cli.github.com/manual/gh_auth_status"),
        ("Both", SETUP, "This API operation needs the admin:public_key scope", "An older sign-in didn't include key permissions.", "Setup asks GitHub for it. Approve it in the browser.", "https://cli.github.com/manual/gh_ssh-key_add"),
        ("Both", SETUP, "key is already in use, Hi someone-else!", "That SSH key belongs to another GitHub account.", "Setup uses HTTPS through your login instead.", "https://docs.github.com/en/authentication/troubleshooting-ssh/error-key-already-in-use"),
        ("Both", SETUP, "REMOTE HOST IDENTIFICATION HAS CHANGED, Host key verification failed", "GitHub's key saved before it changed in 2023.", "Setup refreshes it.", "https://github.blog/news-insights/company-news/we-updated-our-rsa-ssh-host-key/"),
        ("Both", SETUP, "Connection timed out, Connection closed, port 22", "Your network blocks SSH.", "Setup uses GitHub's port 443, then HTTPS.", "https://docs.github.com/en/authentication/troubleshooting-ssh/using-ssh-over-the-https-port"),
        ("Both", SETUP, "Permission denied (publickey)", "A key with a passphrase and no agent, or the key isn't on GitHub.", "Setup uploads it, then falls back to HTTPS. To use the key: add it to your ssh-agent.", "https://docs.github.com/en/authentication/troubleshooting-ssh/error-permission-denied-publickey"),
        ("Both", SETUP, "Bad owner or permissions on", "Windows or macOS thinks the SSH settings are too open.", "Setup repairs them, then falls back to HTTPS.", "https://stackoverflow.com/a/50624841"),
        ("Windows", YOU, "unable to start ssh-agent service, error :1058", "The agent service is disabled. Only needed for a key with a passphrase.", "As administrator: Get-Service ssh-agent | Set-Service -StartupType Manual; Start-Service ssh-agent", "https://docs.github.com/en/authentication/connecting-to-github-with-ssh/generating-a-new-ssh-key-and-adding-it-to-the-ssh-agent"),
        ("Both", YOU, "Load key: invalid format", "The key file is damaged, usually Windows line endings or a BOM.", "Don't overwrite it blindly: ask Kevin. Setup falls back to HTTPS meanwhile.", "https://stackoverflow.com/questions/47859437"),
        ("Mac", YOU, "Requires authentication, HTTP 401 right after signing in", "The keychain failed, so gh has no token to send.", "gh auth login --hostname github.com --web --insecure-storage (stores the token in plain text).", "https://github.com/cli/cli/issues/13317"),
        ("Both", YOU, "forcibly closed by the remote host, connection reset by peer, login/device/code: EOF", "Newer gh builds and a TLS-inspecting network.", "Try another network. On a Mac: GODEBUG=http2client=0 gh auth login --web", "https://github.com/cli/cli/issues/14420"),
    ]),
    ("vscode", "VS Code and extensions", [
        ("Both", SETUP, "Extensions landing in the wrong editor", "Other editors, such as Cursor, also call themselves code.", "Setup looks for the real VS Code first.", ""),
        ("Both", SETUP, "not compatible with the current version of", "An old VS Code.", "Setup upgrades it.", "https://github.com/microsoft/vscode/blob/main/src/vs/platform/extensionManagement/common/abstractExtensionManagementService.ts"),
        ("Both", YOU, "XHR failed, Failed Installing Extensions, Error while installing extensions", "A proxy or a blocked Marketplace. VS Code's command line has limited proxy support.", "Allow marketplace.visualstudio.com, *.gallery.vsassets.io and *.gallerycdn.vsassets.io, or install from inside VS Code. Nothing else depends on this step.", "https://code.visualstudio.com/docs/setup/network"),
        ("Mac", SETUP, "code: command not found, There was a problem with the editor", "VS Code's shell command isn't installed.", "Setup uses its full path.", "https://forum.codewithmosh.com/t/configuration-issue/23541"),
    ]),
    ("uv", "uv, Python 3.12 and pre-commit", [
        ("Both", SETUP, "'pre-commit' is not recognized, command not found", "uv's tools folder isn't on PATH.", "Setup runs uv tool update-shell. Open a new terminal to see it.", "https://docs.astral.sh/uv/concepts/tools/"),
        ("Both", SETUP, "invalid peer certificate: UnknownIssuer", "Your network inspects secure traffic.", "Setup tells uv to use the system's certificates.", "https://docs.astral.sh/uv/concepts/authentication/certificates/"),
        ("Both", YOU, "Failed to download python-build-standalone", "Usually a proxy.", "Set HTTPS_PROXY for your user, or try another network.", "https://docs.astral.sh/uv/reference/environment/"),
        ("Both", SETUP, "Executable already exists: pre-commit", "An older copy, for example from pipx.", "Setup overwrites it.", "https://docs.astral.sh/uv/concepts/tools/"),
        ("Windows", YOU, "Access is denied. (os error 5), Failed to update Windows PE resources", "Antivirus or endpoint protection.", "Add an exclusion. If Python vanished: uv python install --reinstall 3.12", "https://github.com/astral-sh/uv/issues/20792"),
        ("Windows", YOU, "The filename or extension is too long", "Long paths are off.", "As administrator: New-ItemProperty -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\FileSystem' -Name LongPathsEnabled -Value 1 -PropertyType DWORD -Force", "https://github.com/astral-sh/uv/issues/7436"),
    ]),
    ("dbeaver", "DBeaver", [
        ("Both", YOU, "Do you want to create sample database?", "DBeaver's first start. This is how queries end up in the wrong place.", "Press No.", "https://github.com/dbeaver/dbeaver/blob/devel/plugins/org.jkiss.dbeaver.ui.config.sample/src/org/jkiss/dbeaver/ui/config/sample/WorkbenchInitializerCreateSampleDatabase.java"),
        ("Both", YOU, "Can't lock workspace at", "DBeaver is already starting.", "Press Abort, never Ignore, then run the file again.", "https://github.com/dbeaver/dbeaver/issues/39293"),
        ("Both", YOU, "[SQLITE_ERROR] no such table: wms.order_header", "The script is on DBeaver Sample Database.", "Open a new script from a Harborline connection.", ""),
        ("Both", YOU, "relation \"dbo.custtable\" does not exist", "The script is on the wrong database. AX tables are in ax2012.", "Open a script from Harborline AX 2012, or switch with Ctrl+0.", ""),
        ("Both", YOU, "cross-database references are not implemented", "A Postgres query reaches one database only.", "Join them in your platform. To explore, DuckDB can attach both read-only.", "https://www.postgresql.org/docs/current/ddl-schemas.html"),
        ("Both", YOU, "password authentication failed for user", "A local PostgreSQL answered on that port, or the password changed after the database was created.", "Setup moves its own port away from anything it can see. If you moved it later, edit the port in the connection.", "https://dev.to/dcs-soni/the-silent-database-conflict-boc"),
        ("Both", YOU, "Check that the hostname and port are correct and that the postmaster is accepting TCP/IP connections", "The containers are down, for example after a restart.", "Run start.bat (Mac: ./start.sh) in the simulator folder.", "https://github.com/pgjdbc/pgjdbc/blob/master/pgjdbc/src/main/java/org/postgresql/core/v3/ConnectionFactoryImpl.java"),
        ("Both", YOU, "Maven artifact not found, Error resolving dependencies", "DBeaver can't download its PostgreSQL driver, usually a proxy.", "Set a proxy under Preferences, Connections, or add the driver jar by hand.", "https://github.com/dbeaver/dbeaver/issues/3780"),
    ]),
    ("copying", "Settings, ports and files", [
        ("Windows", SETUP, "ports are not available, forbidden by its access permissions", "Windows reserved that port for Hyper-V or WinNAT, with nothing listening on it.", "Setup tests by binding, and picks a free port.", "https://github.com/docker/for-win/issues/9272"),
        ("Both", SETUP, "port is already allocated, address already in use, Only one usage of each socket", "A local PostgreSQL, IIS or Postgres.app has the port.", "Setup moves its own port in .env and never stops your service.", "https://docs.docker.com/desktop/troubleshoot-and-support/troubleshoot/topics/"),
        ("Both", YOU, "no configuration file provided: not found", "docker compose was run outside the simulator folder.", "Change to the simulator folder first.", ""),
        ("Windows", SETUP, "bad interpreter, $'\\r': command not found", "Windows line endings in a script that runs in Linux.", "Setup normalises the database scripts when it copies them.", "https://docs.docker.com/desktop/troubleshoot-and-support/troubleshoot/topics/"),
        ("Mac", YOU, "Mounts denied, is not shared from the host", "The folder is outside Docker's shared locations.", "Docker Desktop, Settings, Resources, File sharing: add it.", "https://github.com/docker/for-mac/issues/6243"),
    ]),
    ("simulator", "Starting the simulator", [
        ("Both", SETUP, "TLS handshake timeout, i/o timeout, no such host, ECONNRESET, ETIMEDOUT, EAI_AGAIN", "A network problem during a download.", "Setup waits and retries twice. If it repeats, set a DNS server in Docker Engine settings.", "https://forums.docker.com/t/npm-ci-error-while-docker-compose-build-no-cache-processing/148876"),
        ("Both", YOU, "toomanyrequests, 429 Too Many Requests, pull rate limit", "Docker Hub allows 100 anonymous pulls per 6 hours per network, shared on VPNs and offices.", "Wait, change network, or run docker login.", "https://docs.docker.com/docker-hub/usage/pulls/"),
        ("Both", SETUP, "error getting credentials, docker-credential-desktop not found", "Docker's settings name a login helper that's missing.", "Setup turns it off and keeps a backup of the file.", "https://forums.docker.com/t/error-storing-credentials-err-exec-docker-credential-desktop-executable-file-not-found-in-path/148439"),
        ("Both", SETUP, "no space left on device", "Docker's disk is full.", "Setup clears the build cache only. If it persists: free disk space, or raise Settings, Resources, Disk usage limit.", "https://docs.docker.com/reference/cli/docker/builder/prune/"),
        ("Both", SETUP, "dependency failed to start, is unhealthy", "Postgres is slow, for example replaying after a crash.", "Setup waits and retries. Its log goes in the report.", "https://kodekloud.com/community/t/docker-compose-example-voting-app-redis-postgres-fail-to-start/255247"),
        ("Both", YOU, "x509: certificate signed by unknown authority", "Your network inspects secure traffic.", "Ask IT for the root certificate, or try another network.", "https://docs.docker.com/guides/zscaler/"),
        ("Both", YOU, "failed to fetch oauth token, failed to authorize", "A stale Docker Hub login.", "Run docker logout, then the file again.", "https://forums.docker.com/t/failed-to-authorize-failed-to-fetch-oauth-token/145067"),
        ("Both", YOU, "env_file.0 must be a string, 'compose' is not a docker command", "Docker Compose is older than 2.24.", "Update Docker Desktop.", "https://docs.docker.com/compose/how-tos/environment-variables/set-environment-variables/"),
        ("Windows", YOU, "error from sender, Access is denied", "Something blocks reading a file, usually antivirus.", "Grant read access to the path it names, or add an exclusion.", "https://forums.docker.com/t/access-is-denied-when-trying-to-build-a-docker-image-with-selenium-standalone-chrome/125082"),
        ("Both", YOU, "certificate has expired or is not yet valid", "Docker's clock drifted after the computer slept.", "Restart Docker Desktop.", "https://github.com/docker/for-win/issues/4526"),
    ]),
    ("build", "Building the data", [
        ("Both", YOU, "exit code: 137, OOMKilled, terminated by signal 9", "It ran out of memory.", "Close other apps, raise the memory limit (Windows: memory= in .wslconfig; Mac: Docker Desktop settings), or build Medium in the Control Center.", "https://learn.microsoft.com/en-us/windows/wsl/wsl-config"),
        ("Both", YOU, "could not resize shared memory segment", "Postgres needs more shared memory. Unlikely with this setup.", "Raise shm_size in docker-compose.yml.", "https://raw.githubusercontent.com/docker-library/docs/master/postgres/README.md"),
        ("Both", SETUP, "The app stopped answering for 10 minutes", "The app or Docker went away during the build.", "Setup stops waiting and reports the app's log. Nothing is wiped and a running build keeps going. Run setup again later.", ""),
        ("Both", SETUP, "The app did not answer for 5 minutes", "The app is busy or stuck.", "Setup never starts a build on silence, so your data is safe. It stops with a report. Wait a few minutes and run it again.", ""),
        ("Both", SETUP, "Finished data already exists and a build would wipe it", "Something asked for a build while a finished build exists.", "The app refuses, and setup never replaces finished data. To rebuild on purpose, use Build on the Control Center, which asks you to type REBUILD.", ""),
        ("Both", SETUP, "A data build is running: Build large (seed ...), started ...", "This version of setup needs the app restarted, and a build is in progress.", "Setup asks first. K keeps the build and leaves the update for later. R stops it, restarts on this version and lets you choose a size.", ""),
        ("Both", SETUP, "Permission denied: '/data/landing/edi', The app cannot write to the landing folder", "The landing folder was deleted and Docker recreated it owned by root.", "The app repairs the folder each time it starts, and setup restarts it for you. A build checks the folder first, so finished data is not wiped. Still stuck: check your antivirus.", ""),
        ("Both", YOU, "The data build is taking far too long, or you picked the wrong size", "Build time depends on your computer: Medium is about an hour on a typical laptop, Large about three.", "Press Cancel build on the Control Center, then run setup again and choose another size. Cancelling leaves incomplete data, which is not counted as built.", ""),
        ("Both", YOU, "The drive is nearly full after a build", "Large takes about 30 GB. Docker's disk file does not shrink by itself, but the next build reuses that space.", "Free some space on the drive, or build a smaller size. Ask Kevin before deleting any Docker files.", ""),
    ]),
    ("repo", "Your repository on GitHub", [
        ("Both", SETUP, "already exists and is not an empty directory", "The repository is on GitHub and your folder has files in it.", "Setup connects the folder and leaves your files alone.", "https://git-scm.com/docs/git-clone"),
        ("Both", SETUP, "src refspec main does not match any", "An earlier attempt left a branch called master.", "Setup renames it.", "https://ataiva.com/solved-error-src-refspec-main-does-not-match"),
        ("Both", SETUP, "GH007, push declined due to email privacy restrictions", "GitHub hides your email and blocks pushes that show it.", "Setup uses your no-reply address for this repository.", "https://techielass.com/git-error-your-push-would-publish-a-private-email-address"),
        ("Both", YOU, "fetch first, non-fast-forward, Updates were rejected, unrelated histories", "Your folder and GitHub have different commits.", "Setup won't overwrite either. Ask Kevin. Never force-push.", "https://docs.github.com/en/get-started/using-git/dealing-with-non-fast-forward-errors"),
        ("Both", YOU, "Verify your email before creating repositories", "New GitHub accounts must verify their email first.", "Verify it at github.com/settings/emails, then run the file again.", "https://docs.github.com/en/account-and-profile/reference/email-addresses-reference"),
        ("Both", YOU, "GH013, Changes must be made through a pull request", "main is protected, as intended.", "Push a branch and open a pull request.", "https://forum.modular.com/t/the-modular-community-repo-seems-to-be-broken/2849"),
        ("Both", YOU, "GH013, Push cannot contain secrets", "GitHub found a secret in the commit.", "Remove that file, then git commit --amend --all", "https://docs.github.com/en/code-security/secret-scanning/working-with-secret-scanning-and-push-protection/working-with-push-protection-from-the-command-line"),
        ("Both", YOU, "Upgrade to GitHub Pro or make this repository public", "An earlier repository is private on a free account, where rulesets aren't available.", "Make it public, or skip this: nothing else depends on it.", "https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets"),
    ]),
    ("hooks", "Commit checks in your repository", [
        ("Both", SETUP, "Cowardly refusing to install hooks with core.hooksPath set (in this repository)", "A setting in this repository only.", "Setup clears it.", "https://github.com/pre-commit/pre-commit/blob/main/pre_commit/commands/install_uninstall.py"),
        ("Both", YOU, "Cowardly refusing to install hooks with core.hooksPath set (global)", "A global Git setting, which may be company policy.", "See it with: git config --show-origin --get-all core.hooksPath. Ask before removing it. Nothing else depends on this step.", "https://github.com/pre-commit/pre-commit/blob/main/pre_commit/commands/install_uninstall.py"),
        ("Both", SETUP, "CERTIFICATE_VERIFY_FAILED at go.dev/dl", "The secret scanner needs Go, and its download failed through a TLS-inspecting network.", "Setup installs Go separately, so the download is skipped.", "https://github.com/pre-commit/pre-commit/issues/3538"),
    ]),
    ("chats", "Your team chats", [
        ("Both", SETUP, "That is the site's start page, not a chat", "The address only becomes the chat's own after the first reply.", "Wait for the reply, then copy the address again.", ""),
        ("Both", SETUP, "That is a temporary chat", "Temporary chats can't be reopened.", "Start a normal chat.", "https://help.openai.com/en/articles/8914046-temporary-chat-faq"),
        ("Both", YOU, "The site won't take the zip", "Opening a zip needs a site that can run code on attachments, which may need a paid plan.", "Paste the prompt without it, and attach single files when a question needs them. Iris needs a site that can run code on attachments.", ""),
    ]),
]

WHO = {
    SETUP: '<span class="hb-chip hb-chip--good">Setup fixes this</span>',
    YOU: '<span class="hb-chip hb-chip--warn">You do this</span>',
}


def row(item: tuple[str, str, str, str, str, str]) -> str:
    where, who, error, why, action, source = item
    link = f' <a href="{escape(source)}" rel="noopener">Source</a>' if source else ""
    return (
        f"<tr><td><code>{escape(error)}</code><br><span class=\"muted\">{escape(where)}</span></td>"
        f"<td>{escape(why)}</td><td>{WHO[who]} {escape(action)}{link}</td></tr>"
    )


def render() -> str:
    total = sum(len(items) for _, _, items in GROUPS)
    fixed = sum(1 for _, _, items in GROUPS for item in items if item[1] == SETUP)
    sections = []
    for anchor, title, items in GROUPS:
        rows = "\n".join(row(i) for i in items)
        sections.append(
            f'<section data-section="{anchor}" data-no-progress data-ki-group>\n<h2 id="{anchor}">{escape(title)}</h2>\n'
            '<div class="hb-table-wrap"><table class="hb-table hb-table--dense"><thead><tr><th>What you see</th><th>Why</th>'
            f"<th>What happens</th></tr></thead><tbody>\n{rows}\n</tbody></table></div>\n</section>"
        )
    body = "\n\n".join(sections)
    return f"""<!doctype html>
<html lang="en" data-hb-page="known-issues">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Known issues · Harborline handbook</title>
<meta name="description" content="Every known error for every setup step on Windows and Mac: what it means, and whether the setup file fixes it or what you do.">
<!-- Generated by simulator/harborline/tools/build_known_issues.py. Edit that file, not this one. -->
<script>try{{var t=localStorage.getItem("harborline.theme");if(t==="light"||t==="dark")document.documentElement.setAttribute("data-theme",t)}}catch(e){{}}</script>
<link rel="stylesheet" href="../brand/fonts.css">
<link rel="stylesheet" href="../brand/tokens.css">
<link rel="stylesheet" href="assets/handbook.css">
<link rel="icon" href="../brand/logo/favicon.svg" type="image/svg+xml">
<script src="assets/handbook.js" defer></script>
<style>
  .ki-search {{ display: block; margin: 0 0 8px; }}
  .ki-search input {{ width: 100%; height: 40px; padding: 0 12px; font: inherit; color: var(--c-text); background: var(--c-surface); border: 1px solid var(--c-border); border-radius: 8px; }}
  .ki-search input:focus-visible {{ outline: 2px solid var(--c-focus); outline-offset: 1px; }}
  .hb-content table code {{ white-space: normal; overflow-wrap: anywhere; }}
  [data-ki-hidden] {{ display: none; }}
</style>
</head>
<body>
<div class="hb-shell">
<aside class="hb-side" data-hb-side></aside>
<main class="hb-sheet" id="main"><div class="hb-page hb-page--single">
<article class="hb-content">
<h1>Known issues</h1>
<p class="hb-lead">Every error we know of, for every setup step. Paste part of yours below. Of these {total}, the setup file fixes {fixed}
by itself; for the rest it stops and says what to do. Whatever it is, you can always run the setup file again: it carries
on where it stopped.</p>
<label class="ki-search"><span class="visually-hidden">Search known issues</span>
<input type="search" data-ki-search placeholder="Paste part of your error, like 0x80370102 or port is already allocated"></label>
<p class="muted" data-ki-count aria-live="polite"></p>
<div class="callout callout--note">
<p class="callout-title">Not listed?</p>
<p>Paste setup's report to <a href="team.html#kevin-ohara">Kevin O’Hara, IT help</a>, or to any AI chat. The report is at
<code>%LOCALAPPDATA%\\HarborlineLab\\setup-error.txt</code> (Mac: <code>~/Library/Logs/HarborlineLab/setup-error.txt</code>).</p>
</div>

{body}
</article>
</div></main>
</div>
<script>
(function () {{
  var input = document.querySelector("[data-ki-search]");
  var count = document.querySelector("[data-ki-count]");
  var groups = Array.prototype.slice.call(document.querySelectorAll("[data-ki-group]"));
  function apply() {{
    var words = input.value.trim().toLowerCase().split(/\\s+/).filter(Boolean);
    var shown = 0;
    groups.forEach(function (group) {{
      var any = false;
      group.querySelectorAll("tbody tr").forEach(function (row) {{
        var text = row.textContent.toLowerCase();
        var hit = words.every(function (w) {{ return text.indexOf(w) >= 0; }});
        if (hit) {{ row.removeAttribute("data-ki-hidden"); any = true; shown++; }} else {{ row.setAttribute("data-ki-hidden", ""); }}
      }});
      if (any) group.removeAttribute("data-ki-hidden"); else group.setAttribute("data-ki-hidden", "");
    }});
    count.textContent = words.length ? shown + (shown === 1 ? " match" : " matches") : "";
  }}
  input.addEventListener("input", apply);
}})();
</script>
</body>
</html>
"""


def main() -> None:
    out = DEFAULT_ROOT / OUTPUT
    out.write_text(render(), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
