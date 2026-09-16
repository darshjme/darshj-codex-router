#!/usr/bin/env python3
"""token-diet plugin for Darshj's Codex Router.

Applies the token-diet settings to the CLI agents on this Mac and wires them to the
memory bus. Every file it touches is backed up once as <file>.pre-token-diet-<date>
and restored by `uninstall`. It never deletes user data: skills are moved to an
archive directory, config keys are added or changed with tomlkit/json, and any key
this plugin did not write is left alone.

Subcommands
  status      show which surfaces are already on the diet (read-only)
  apply       apply everything (or a subset with --only codex,grok,claude,djcode,hooks)
  uninstall   restore the backups taken by apply

Surfaces
  codex   ~/.codex/config.toml: drop service_tier=priority, compaction ceiling, tool
          output cap, skills budget, apps instructions off; ~/.codex/AGENTS.md rewritten
          only if --agents-md is given (the shipped template is a starting point).
  grok    ~/.grok/config.toml: skills ignore list, pruning/flush, MCP output cap, model
          defaults; ~/.grok/rules/00-memory.md.
  claude  ~/.claude/settings.json: effort medium, autoCompactWindow, bashOutputMaxChars,
          MAX_MCP_OUTPUT_TOKENS, autocompact percentage; archives never-used skill
          groups given with --archive-skills (default: none).
  djcode  ~/.djcode/bin/djcode shim that isolates DJcode from ~/.claude and caps context.
  hooks   memory-bus recall hooks for Claude Code (UserPromptSubmit) and Codex
          (SessionStart), only when ~/.local/share/memory-bus/bin/memory-bus exists.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import time
from pathlib import Path

try:
    import tomlkit
except ImportError:  # the router's own requirements.txt provides tomlkit
    print("tomlkit is required: pip install tomlkit  (or run with the router's .venv)", file=sys.stderr)
    sys.exit(2)

HERE = Path(__file__).resolve().parent
HOME = Path.home()
DATE = time.strftime("%Y%m%d")
SUFFIX = ".pre-token-diet-" + DATE
MEMORY_BUS_CLIENT = HOME / ".local/share/memory-bus/bin/memory-bus"

CODEX_CONFIG = HOME / ".codex/config.toml"
CODEX_AGENTS = HOME / ".codex/AGENTS.md"
CODEX_HOOKS = HOME / ".codex/hooks.json"
GROK_CONFIG = HOME / ".grok/config.toml"
GROK_RULES = HOME / ".grok/rules/00-memory.md"
CLAUDE_SETTINGS = HOME / ".claude/settings.json"
CLAUDE_SKILLS = HOME / ".claude/skills"
CLAUDE_SKILLS_ARCHIVE = HOME / ".claude/skills-archive"
DJCODE_SHIM = HOME / ".djcode/bin/djcode"
DJCODE_CONFIG = HOME / ".djcode-config"

# Values measured and applied on 2026-09-16 (docs/FINDINGS.md). Change here, not in the code below.
CODEX_KEYS = {
    "model_auto_compact_token_limit": 110000,
    "model_auto_compact_token_limit_scope": "total",
    "tool_output_token_limit": 8000,
    "project_doc_max_bytes": 4096,
    "include_apps_instructions": False,
}
CODEX_SKILLS_MAX_CONTEXT_TOKENS = 1200
GROK_TABLES = {
    "models": {"default_reasoning_effort": "medium"},
    "session": {"auto_compact_threshold_percent": 30},
    "compaction": {
        "pruning": {"enabled": True, "keep_last_n_turns": 3, "soft_trim_threshold": 3000,
                    "soft_trim_head": 1200, "soft_trim_tail": 800, "hard_clear_age_turns": 8},
        "memory_flush": {"enabled": True, "soft_threshold_tokens": 4000, "max_flush_write_chars": 4000,
                         "idle_timeout_secs": 0},
    },
    "mcp": {"max_output_bytes": 20000},
    "skills": {"ignore": ["~/.agents/skills", "~/.claude/skills"]},
    "compat": {"claude": {"skills": False, "hooks": False, "mcps": False, "agents": True}},
}
CLAUDE_KEYS = {"effortLevel": "medium", "autoCompactWindow": 200000, "bashOutputMaxChars": 30000}
CLAUDE_ENV = {"MAX_MCP_OUTPUT_TOKENS": "20000", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "80",
              "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}


# ----------------------------------------------------------------------------- helpers
def backup(path: Path) -> Path | None:
    """Copy path to path<SUFFIX> once. Returns the backup path (None if the file is absent)."""
    if not path.exists():
        return None
    dest = path.with_name(path.name + SUFFIX)
    if not dest.exists():
        shutil.copy2(path, dest)
    return dest


def restore(path: Path) -> bool:
    """Restore the newest .pre-token-diet-* backup of path. Returns True if restored."""
    if not path.parent.exists():
        return False
    backups = sorted(path.parent.glob(path.name + ".pre-token-diet-*"))
    if not backups:
        return False
    shutil.copy2(backups[-1], path)
    return True


def atomic_write(path: Path, text: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-token-diet")
    tmp.write_text(text, encoding="utf-8")
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def load_toml(path: Path) -> tomlkit.TOMLDocument:
    return tomlkit.parse(path.read_text(encoding="utf-8")) if path.exists() else tomlkit.document()


def set_nested(doc, table_path: list[str], values: dict) -> int:
    """Set keys inside doc[table_path...] (creating tables). Returns number of keys changed."""
    node = doc
    for name in table_path:
        if name not in node:
            node[name] = tomlkit.table()
        node = node[name]
    changed = 0
    for key, value in values.items():
        if isinstance(value, dict):
            changed += set_nested(node, [key], value)
        elif key not in node or node[key] != value:
            node[key] = value
            changed += 1
    return changed


def render(template: Path) -> str:
    return template.read_text(encoding="utf-8").replace("$HOME", str(HOME))


def say(msg: str) -> None:
    print(msg, flush=True)


# ----------------------------------------------------------------------------- codex
def codex_status() -> dict:
    if not CODEX_CONFIG.exists():
        return {"present": False}
    doc = load_toml(CODEX_CONFIG)
    return {
        "present": True,
        "service_tier": doc.get("service_tier"),
        "compaction_limit": doc.get("model_auto_compact_token_limit"),
        "tool_output_token_limit": doc.get("tool_output_token_limit"),
        "skills_max_context_tokens": (doc.get("skills") or {}).get("max_context_tokens"),
        "agents_md_bytes": CODEX_AGENTS.stat().st_size if CODEX_AGENTS.exists() else None,
    }


def codex_apply(args) -> None:
    if not CODEX_CONFIG.exists():
        say("codex: ~/.codex/config.toml not found, skipping")
        return
    backup(CODEX_CONFIG)
    doc = load_toml(CODEX_CONFIG)
    changed = 0
    if "service_tier" in doc:
        del doc["service_tier"]
        changed += 1
    changed += set_nested(doc, [], CODEX_KEYS)
    changed += set_nested(doc, ["skills"], {"max_context_tokens": CODEX_SKILLS_MAX_CONTEXT_TOKENS})
    changed += set_nested(doc, ["features"], {"recommended_plugins": False})
    atomic_write(CODEX_CONFIG, tomlkit.dumps(doc))
    say(f"codex: config.toml {changed} key(s) changed (backup {CODEX_CONFIG.name}{SUFFIX})")
    if args.agents_md:
        backup(CODEX_AGENTS)
        atomic_write(CODEX_AGENTS, render(HERE / "configs/codex-AGENTS.md"))
        say(f"codex: AGENTS.md replaced with the template ({CODEX_AGENTS.stat().st_size} bytes)")
    else:
        say("codex: AGENTS.md left alone (pass --agents-md to install the ~1.8 KB template)")


def codex_uninstall() -> None:
    say("codex: config.toml " + ("restored" if restore(CODEX_CONFIG) else "no backup"))
    say("codex: AGENTS.md " + ("restored" if restore(CODEX_AGENTS) else "no backup"))
    say("codex: hooks.json " + ("restored" if restore(CODEX_HOOKS) else "no backup"))


# ----------------------------------------------------------------------------- grok
def grok_status() -> dict:
    if not GROK_CONFIG.exists():
        return {"present": False}
    doc = load_toml(GROK_CONFIG)
    return {
        "present": True,
        "skills_ignore": (doc.get("skills") or {}).get("ignore"),
        "pruning": ((doc.get("compaction") or {}).get("pruning") or {}).get("enabled"),
        "mcp_max_output_bytes": (doc.get("mcp") or {}).get("max_output_bytes"),
        "rules_file": GROK_RULES.exists(),
    }


def grok_apply(args) -> None:
    if not GROK_CONFIG.exists():
        say("grok: ~/.grok/config.toml not found, skipping")
        return
    backup(GROK_CONFIG)
    doc = load_toml(GROK_CONFIG)
    changed = 0
    for table, values in GROK_TABLES.items():
        changed += set_nested(doc, [table], values)
    if args.grok_model:
        changed += set_nested(doc, ["models"], {"default": args.grok_model})
    atomic_write(GROK_CONFIG, tomlkit.dumps(doc))
    say(f"grok: config.toml {changed} key(s) changed (backup {GROK_CONFIG.name}{SUFFIX})")
    if not GROK_RULES.exists():
        atomic_write(GROK_RULES, render(HERE / "configs/grok-rules-memory.md"))
        say("grok: rules/00-memory.md installed")


def grok_uninstall() -> None:
    say("grok: config.toml " + ("restored" if restore(GROK_CONFIG) else "no backup"))
    if GROK_RULES.exists() and GROK_RULES.read_text(encoding="utf-8") == render(HERE / "configs/grok-rules-memory.md"):
        GROK_RULES.unlink()
        say("grok: rules/00-memory.md removed")


# ----------------------------------------------------------------------------- claude code
def claude_load() -> dict:
    return json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8")) if CLAUDE_SETTINGS.exists() else {}


def claude_status() -> dict:
    d = claude_load()
    return {
        "present": CLAUDE_SETTINGS.exists(),
        "effortLevel": d.get("effortLevel"),
        "autoCompactWindow": d.get("autoCompactWindow"),
        "bashOutputMaxChars": d.get("bashOutputMaxChars"),
        "MAX_MCP_OUTPUT_TOKENS": (d.get("env") or {}).get("MAX_MCP_OUTPUT_TOKENS"),
        "recall_hook": any("memory-bus hook claude" in h.get("command", "")
                           for g in (d.get("hooks") or {}).get("UserPromptSubmit", []) for h in g.get("hooks", [])),
        "skills": len(list(CLAUDE_SKILLS.iterdir())) if CLAUDE_SKILLS.is_dir() else 0,
        "archived": len(list(CLAUDE_SKILLS_ARCHIVE.iterdir())) if CLAUDE_SKILLS_ARCHIVE.is_dir() else 0,
    }


def claude_apply(args) -> None:
    backup(CLAUDE_SETTINGS)
    d = claude_load()
    changed = 0
    for k, v in CLAUDE_KEYS.items():
        if d.get(k) != v:
            d[k] = v
            changed += 1
    env = d.setdefault("env", {})
    for k, v in CLAUDE_ENV.items():
        if env.get(k) != v:
            env[k] = v
            changed += 1
    # Explicit per-model xhigh defaults are the expensive part; cap them at high.
    for model, ms in (d.get("modelSettings") or {}).items():
        if ms.get("effortLevel") in ("xhigh", "max"):
            ms["effortLevel"] = "high"
            changed += 1
    atomic_write(CLAUDE_SETTINGS, json.dumps(d, indent=2) + "\n")
    say(f"claude: settings.json {changed} key(s) changed (backup {CLAUDE_SETTINGS.name}{SUFFIX})")
    moved = 0
    for pattern in args.archive_skills:
        for entry in sorted(CLAUDE_SKILLS.glob(pattern)) if CLAUDE_SKILLS.is_dir() else []:
            if entry.name in args.keep_skills:
                continue
            CLAUDE_SKILLS_ARCHIVE.mkdir(parents=True, exist_ok=True)
            shutil.move(str(entry), str(CLAUDE_SKILLS_ARCHIVE / entry.name))
            moved += 1
    if moved:
        say(f"claude: {moved} skill(s) moved to {CLAUDE_SKILLS_ARCHIVE} (move them back to undo)")


def claude_uninstall() -> None:
    say("claude: settings.json " + ("restored" if restore(CLAUDE_SETTINGS) else "no backup"))
    if CLAUDE_SKILLS_ARCHIVE.is_dir():
        n = 0
        for entry in sorted(CLAUDE_SKILLS_ARCHIVE.iterdir()):
            target = CLAUDE_SKILLS / entry.name
            if not target.exists():
                shutil.move(str(entry), str(target))
                n += 1
        say(f"claude: {n} archived skill(s) moved back")


# ----------------------------------------------------------------------------- djcode
def djcode_status() -> dict:
    return {"present": DJCODE_SHIM.exists(),
            "isolated": DJCODE_SHIM.exists() and "CLAUDE_CONFIG_DIR" in DJCODE_SHIM.read_text(encoding="utf-8"),
            "config_dir": DJCODE_CONFIG.is_dir()}


def djcode_apply(args) -> None:
    if not DJCODE_SHIM.exists():
        say("djcode: ~/.djcode/bin/djcode not found, skipping")
        return
    backup(DJCODE_SHIM)
    atomic_write(DJCODE_SHIM, render(HERE / "configs/djcode-shim.sh"), mode=0o755)
    DJCODE_CONFIG.mkdir(parents=True, exist_ok=True)
    if not (DJCODE_CONFIG / "settings.json").exists():
        settings = {"env": {"BASH_MAX_OUTPUT_LENGTH": "12000", "MAX_MCP_OUTPUT_TOKENS": "8000"}, "effortLevel": "medium"}
        if MEMORY_BUS_CLIENT.exists():
            settings["hooks"] = json.loads(render(HERE / "configs/hooks/djcode-settings.json"))["hooks"]
        atomic_write(DJCODE_CONFIG / "settings.json", json.dumps(settings, indent=2) + "\n")
    if not (DJCODE_CONFIG / "CLAUDE.md").exists():
        atomic_write(DJCODE_CONFIG / "CLAUDE.md", "# DJcode working rules\n"
                     "- Local-first; keep context small: read only the files you need.\n"
                     "- Memory is retrieved, not loaded: `memory-bus recall \"<topic>\" --max-tokens 800`.\n"
                     "- Large tool output: `memory-bus compress --target 800` before reasoning over it.\n"
                     "- No secrets in notes. Archive, never delete user data.\n")
    say(f"djcode: shim isolated to {DJCODE_CONFIG} (backup {DJCODE_SHIM.name}{SUFFIX})")


def djcode_uninstall() -> None:
    say("djcode: shim " + ("restored" if restore(DJCODE_SHIM) else "no backup"))
    if DJCODE_SHIM.exists():
        os.chmod(DJCODE_SHIM, DJCODE_SHIM.stat().st_mode | stat.S_IXUSR)


# ----------------------------------------------------------------------------- hooks
def hooks_apply(args) -> None:
    if not MEMORY_BUS_CLIENT.exists():
        say(f"hooks: {MEMORY_BUS_CLIENT} missing; run memory-bus/mac/install-mac.sh first. Skipping.")
        return
    # Claude Code: one UserPromptSubmit hook appended to whatever is already there.
    backup(CLAUDE_SETTINGS)
    d = claude_load()
    hook = json.loads(render(HERE / "configs/hooks/claude-settings.hooks.json"))["hooks"]["UserPromptSubmit"][0]
    groups = d.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
    if not any("memory-bus hook claude" in h.get("command", "") for g in groups for h in g.get("hooks", [])):
        groups.append(hook)
        atomic_write(CLAUDE_SETTINGS, json.dumps(d, indent=2) + "\n")
        say("hooks: Claude Code UserPromptSubmit recall hook added")
    else:
        say("hooks: Claude Code recall hook already present")
    # Codex: only if no hooks.json exists. Codex pins each hook command with a trusted_hash in
    # config.toml [hooks.state]; rewriting an existing command silently disables that hook until it
    # is re-approved in an interactive session, so an existing file is reported, not edited.
    if CODEX_HOOKS.exists():
        say("hooks: ~/.codex/hooks.json exists; not touching it (see README: trusted_hash). "
            "Add the SessionStart recall by calling `memory-bus hook codex` from your existing hook script.")
    else:
        atomic_write(CODEX_HOOKS, render(HERE / "configs/hooks/codex-hooks.json"))
        say("hooks: ~/.codex/hooks.json installed (approve it once in an interactive codex session)")


# ----------------------------------------------------------------------------- cli
SURFACES = ["codex", "grok", "claude", "djcode", "hooks"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    a = sub.add_parser("apply")
    a.add_argument("--only", default=",".join(SURFACES), help="comma list of surfaces (default: all)")
    a.add_argument("--agents-md", action="store_true", help="replace ~/.codex/AGENTS.md with the shipped template")
    a.add_argument("--grok-model", default="", help="set [models].default in ~/.grok/config.toml")
    a.add_argument("--archive-skills", action="append", default=[],
                   help="glob under ~/.claude/skills to move into ~/.claude/skills-archive (repeatable)")
    a.add_argument("--keep-skills", action="append", default=[], help="skill names exempt from --archive-skills")
    u = sub.add_parser("uninstall")
    u.add_argument("--only", default=",".join(SURFACES))
    args = ap.parse_args(argv)

    if args.cmd == "status":
        print(json.dumps({"codex": codex_status(), "grok": grok_status(), "claude": claude_status(),
                          "djcode": djcode_status(), "memory_bus_client": MEMORY_BUS_CLIENT.exists()}, indent=2))
        return 0
    only = [s.strip() for s in args.only.split(",") if s.strip()]
    unknown = [s for s in only if s not in SURFACES]
    if unknown:
        ap.error("unknown surface(s): " + ", ".join(unknown))
    if args.cmd == "apply":
        for s in only:
            {"codex": codex_apply, "grok": grok_apply, "claude": claude_apply,
             "djcode": djcode_apply, "hooks": hooks_apply}[s](args)
    else:
        for s in only:
            {"codex": codex_uninstall, "grok": grok_uninstall, "claude": claude_uninstall,
             "djcode": djcode_uninstall, "hooks": lambda: say("hooks: restored with the claude/codex backups")}[s]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
