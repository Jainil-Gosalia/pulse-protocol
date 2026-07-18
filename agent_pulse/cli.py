"""agent-pulse CLI: serve the collector, install tool adapters, check status.

    agent-pulse serve [--host H] [--port P] [--token T]
    agent-pulse install claude_code|opencode|kilo|codex|cursor [--project] [--yes]
    agent-pulse status
"""
import argparse
import json
import os
import shutil
import sys
import urllib.request
from pathlib import Path

PKG_DIR = Path(__file__).parent
HOOK_PATH = (PKG_DIR / "hook_client.py").resolve()
ADAPTERS = PKG_DIR / "adapters"

CLAUDE_HOOK_EVENTS = ["SessionStart", "SessionEnd", "UserPromptSubmit",
                      "Notification", "Stop", "SubagentStop",
                      "PreToolUse", "PostToolUse"]
PULSE_MARKER = "hook_client.py"  # how we recognize our own entries


def _hook_command() -> str:
    python = Path(sys.executable).as_posix()
    return f'"{python}" "{HOOK_PATH.as_posix()}"'


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _backup(path: Path) -> None:
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".pulse-backup"))


def install_claude_code(project: bool, yes: bool) -> int:
    target = (Path.cwd() / ".claude" if project else Path.home() / ".claude") / "settings.json"
    settings = {}
    if target.exists():
        settings = json.loads(target.read_text(encoding="utf-8"))
    hooks = settings.setdefault("hooks", {})
    command = _hook_command()

    added = []
    for event in CLAUDE_HOOK_EVENTS:
        entries = hooks.setdefault(event, [])
        if any(PULSE_MARKER in json.dumps(e) for e in entries):
            continue  # already installed
        entry = {"hooks": [{"type": "command", "command": command}]}
        if event in ("PreToolUse", "PostToolUse"):
            entry["matcher"] = "*"
        entries.append(entry)
        added.append(event)

    if not added:
        print(f"Already installed in {target}")
        return 0
    print(f"Will add Agent Pulse hooks for {', '.join(added)}")
    print(f"  to: {target}\n  command: {command}")
    if not _confirm("Apply?", yes):
        print("Aborted.")
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    _backup(target)
    target.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    print(f"Done. Backup at {target}.pulse-backup (if it existed). "
          "Restart running Claude Code sessions to pick this up.")
    return 0


def _install_js_plugin(tool: str, project: bool, yes: bool) -> int:
    src = ADAPTERS / ("opencode_plugin.js" if tool == "opencode" else "kilo_plugin.js")
    base = Path.cwd() / f".{tool}" if project else Path.home() / ".config" / tool
    target = base / "plugin" / "agent-pulse.js"
    print(f"Will copy the {tool} plugin to: {target}")
    if not _confirm("Apply?", yes):
        print("Aborted.")
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, target)
    print(f"Done. Restart {tool} to pick this up.")
    return 0


def install_codex(project: bool, yes: bool) -> int:
    notify_path = (ADAPTERS / "codex_notify.py").as_posix()
    python = Path(sys.executable).as_posix()
    print("Codex reads its notify program from ~/.codex/config.toml. "
          "Add (or merge) this line yourself:\n")
    print(f'    notify = ["{python}", "{notify_path}"]\n')
    print("(TOML is not merged automatically to avoid corrupting other settings.)")
    return 0


def install_cursor(project: bool, yes: bool) -> int:
    target = (Path.cwd() if project else Path.home()) / ".cursor" / "hooks.json"
    script = (ADAPTERS / "cursor_hook.py").as_posix()
    python = Path(sys.executable).as_posix()
    command = f'"{python}" "{script}"'
    events = ["beforeSubmitPrompt", "beforeShellExecution", "beforeMCPExecution",
              "afterFileEdit", "stop"]

    config = {"version": 1, "hooks": {}}
    if target.exists():
        config = json.loads(target.read_text(encoding="utf-8"))
        config.setdefault("version", 1)
        config.setdefault("hooks", {})

    added = []
    for event in events:
        entries = config["hooks"].setdefault(event, [])
        if any("cursor_hook.py" in json.dumps(e) for e in entries):
            continue
        entries.append({"command": command})
        added.append(event)

    if not added:
        print(f"Already installed in {target}")
        return 0
    print(f"Will add Agent Pulse cursor hooks for {', '.join(added)}\n  to: {target}")
    if not _confirm("Apply?", yes):
        print("Aborted.")
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    _backup(target)
    target.write_text(json.dumps(config, indent=2), encoding="utf-8")
    print(f"Done. Backup at {target}.pulse-backup (if it existed).")
    return 0


INSTALLERS = {
    "claude_code": install_claude_code,
    "opencode": lambda p, y: _install_js_plugin("opencode", p, y),
    "kilo": lambda p, y: _install_js_plugin("kilo", p, y),
    "codex": install_codex,
    "cursor": install_cursor,
}


def cmd_serve(args) -> int:
    # Under pythonw (autostart) there is no console: stdout/stderr are None
    # and uvicorn's logging would crash on startup. Log to a file instead.
    if sys.stdout is None or sys.stderr is None:
        log_path = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".") / "agent-pulse.log"
        log = open(log_path, "a", buffering=1, encoding="utf-8", errors="replace")
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log
    if args.token:
        os.environ["PULSE_COLLECTOR_TOKEN"] = args.token
    # Let the collector know its bind host so the Connect-phone panel can tell
    # whether LAN/Tailscale URLs are actually reachable.
    os.environ["PULSE_BOUND_HOST"] = args.host
    import uvicorn
    from agent_pulse.collector.main import app
    if args.host != "127.0.0.1" and not os.environ.get("PULSE_COLLECTOR_TOKEN"):
        print("WARNING: binding beyond loopback without --token / "
              "PULSE_COLLECTOR_TOKEN exposes your agent activity to the network.")
    if getattr(args, "tunnel", False):
        if not os.environ.get("PULSE_COLLECTOR_TOKEN"):
            print("--tunnel requires a token (the URL is public). Use --token.")
            return 1
        exe = shutil.which("cloudflared")
        if not exe:
            print("--tunnel needs cloudflared. Install once:\n"
                  "  winget install Cloudflare.cloudflared\nthen rerun.")
            return 1
        import subprocess
        import threading
        import re
        proc = subprocess.Popen([exe, "tunnel", "--url", f"http://127.0.0.1:{args.port}"],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        def _watch():
            for line in proc.stdout:
                m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
                if m:
                    tok = os.environ.get("PULSE_COLLECTOR_TOKEN", "")
                    print(f"\n=== Phone URL (any network): {m.group(0)}/?token={tok} ===\n",
                          flush=True)

        threading.Thread(target=_watch, daemon=True).start()
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def cmd_autostart(args) -> int:
    """Start the collector automatically at login (Windows Startup folder)."""
    if os.name != "nt":
        print("autostart currently supports Windows only; use a systemd user "
              "unit or launchd agent running 'agent-pulse serve' elsewhere.")
        return 1
    startup = (Path(os.environ["APPDATA"]) / "Microsoft" / "Windows"
               / "Start Menu" / "Programs" / "Startup")
    script = startup / "agent-pulse.vbs"
    if args.remove:
        if script.exists():
            script.unlink()
            print(f"Removed {script}")
        else:
            print("Autostart was not installed.")
        return 0
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    runner = pythonw if pythonw.exists() else Path(sys.executable)
    script.write_text(
        'CreateObject("Wscript.Shell").Run """{}"" -m agent_pulse.cli serve", 0, False\n'
        .format(runner), encoding="utf-8")
    print(f"Installed: {script}\nThe collector will start (hidden) at every "
          "login. Remove with: agent-pulse autostart --remove")
    return 0


def cmd_setup_phone(args) -> int:
    """Open the firewall for phone access via one UAC prompt, print URLs."""
    if os.name != "nt":
        print("Windows only; on other systems allow TCP 8765 in your firewall.")
        return 1
    import subprocess
    import ctypes
    import time
    import socket as sock
    rule = "Agent Pulse (8765)"

    def rule_exists() -> bool:
        return subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", f"name={rule}"],
            capture_output=True).returncode == 0

    if rule_exists():
        print("Firewall: already open")
    else:
        params = (f'advfirewall firewall add rule name="{rule}" dir=in '
                  f'action=allow protocol=TCP localport=8765')
        print("Requesting admin approval to open port 8765 — click Yes on the Windows prompt…")
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "netsh", params, None, 0)
        if rc <= 32:
            print(f"Approval declined/failed. Manual (admin): netsh {params}")
            return 1
        for _ in range(30):
            time.sleep(1)
            if rule_exists():
                break
        print("Firewall: OK" if rule_exists() else "Firewall: rule not visible yet — recheck shortly")

    from agent_pulse import netinfo
    token = os.environ.get("PULSE_COLLECTOR_TOKEN")
    urls = netinfo.reachable_urls(int(os.environ.get("PULSE_PORT", "8765")), token)
    if not urls:
        print("Couldn't detect a reachable address. Is the machine on a network?")
        return 0

    primary = urls[0]["url"]
    print("\nScan on your phone (or open the URL):\n")
    qr = netinfo.qr_terminal(primary)
    if qr:
        try:
            sys.stdout.write(qr + "\n\n")
            sys.stdout.flush()
        except UnicodeEncodeError:
            pass  # console can't render the blocks — the URLs below still work
    for u in urls:
        print(f"  {u['label']:<24} {u['url']}")
    if not token:
        print("\n(no token set — anyone on the network can open this. "
              "Restart with --token for a private link.)")
    return 0


def cmd_conformance(args) -> int:
    from agent_pulse.conformance import run as run_conformance
    return run_conformance(url=args.url, token=args.token)


def cmd_status(args) -> int:
    base = os.environ.get("PULSE_COLLECTOR_URL",
                          "http://127.0.0.1:8765/api/events").rsplit("/api/events", 1)[0]
    headers = {}
    token = os.environ.get("PULSE_COLLECTOR_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(base + "/api/sessions", headers=headers)
        with urllib.request.urlopen(req, timeout=3) as r:
            sessions = json.load(r)
    except Exception as e:
        print(f"Collector not reachable at {base} ({e.__class__.__name__})")
        return 1
    print(f"Collector OK at {base} — {len(sessions)} session(s)")
    for s in sessions:
        print(f"  [{s['status']:<11}] {s['source']:<12} {s['cwd'] or '?'}  ({s['last_summary'] or ''})")
    return 0


def main() -> None:
    # QR codes and box-drawing need UTF-8; Windows consoles default to cp1252.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    parser = argparse.ArgumentParser(prog="agent-pulse", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="run the collector + dashboard")
    p_serve.add_argument("--host", default=os.environ.get("PULSE_HOST", "127.0.0.1"))
    p_serve.add_argument("--port", type=int, default=int(os.environ.get("PULSE_PORT", "8765")))
    p_serve.add_argument("--token", help="require this bearer token on the API "
                         "(default: PULSE_COLLECTOR_TOKEN env var)")
    p_serve.add_argument("--tunnel", action="store_true",
                         help="publish a public HTTPS URL via a Cloudflare quick "
                              "tunnel (no account/VPN/firewall needed)")
    p_serve.set_defaults(func=cmd_serve)

    p_install = sub.add_parser("install", help="install an adapter into a tool's config")
    p_install.add_argument("tool", choices=sorted(INSTALLERS))
    p_install.add_argument("--project", action="store_true",
                           help="install into the current project instead of globally")
    p_install.add_argument("--yes", "-y", action="store_true", help="skip confirmation")
    p_install.set_defaults(func=lambda a: INSTALLERS[a.tool](a.project, a.yes))

    p_status = sub.add_parser("status", help="show collector health and sessions")
    p_status.set_defaults(func=cmd_status)

    p_conf = sub.add_parser("conformance", help="validate a collector against the Pulse spec")
    p_conf.add_argument("--url", help="collector events URL (default: PULSE_COLLECTOR_URL or localhost)")
    p_conf.add_argument("--token", help="bearer token (default: PULSE_COLLECTOR_TOKEN)")
    p_conf.set_defaults(func=cmd_conformance)

    p_phone = sub.add_parser("setup-phone", help="open firewall for phone access (one UAC click)")
    p_phone.set_defaults(func=cmd_setup_phone)

    p_auto = sub.add_parser("autostart", help="run the collector at login")
    p_auto.add_argument("--remove", action="store_true")
    p_auto.set_defaults(func=cmd_autostart)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
