"""Path sandboxing for file tools and the dangerous-command check for shell().

This is a guardrail that makes the agent ask before risky commands, not a sandbox:
a determined model could obfuscate a command. Run the service as an unprivileged user.
"""
from __future__ import annotations

import os
import re
import shlex
from pathlib import Path


class PathError(Exception):
    pass


def resolve_in_workdir(workdir: Path, path: str) -> Path:
    """Resolve path relative to workdir and refuse anything that escapes it (incl. symlinks)."""
    raw = os.path.expanduser(path or ".")
    p = Path(raw)
    if not p.is_absolute():
        p = workdir / p
    real = Path(os.path.realpath(p))
    root = Path(os.path.realpath(workdir))
    if real != root and root not in real.parents:
        raise PathError(f"path is outside WORKDIR: {path}")
    return real


# A word in command position: start of line or after ; & | ( ` $( or common prefixes.
_CMD = r"(?:^|[;&|(`\n]\s*|\b(?:env|exec|nohup|xargs|time|nice|then|do)\s+)"

DANGEROUS_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("sudo/su", re.compile(r"(?:^|[\s;&|(`])(?:sudo|su|doas|pkexec)(?:\s|$)")),
    ("shutdown/reboot", re.compile(_CMD + r"(?:shutdown|reboot|halt|poweroff|init\s+[06])\b")),
    ("pipe to shell", re.compile(r"\b(?:curl|wget)\b[^\n]*\|\s*(?:sudo\s+)?(?:\S*/)?"
                                 r"(?:ba|z|da|k|fi)?sh\b|(?:ba|z)?sh\s+<\(\s*(?:curl|wget)")),
    ("dd", re.compile(_CMD + r"dd\s")),
    ("mkfs/disk tools", re.compile(r"\b(?:mkfs(?:\.\w+)?|mkswap|fdisk|parted|wipefs)\b")),
    ("chmod 777", re.compile(r"\bchmod\s+(?:-\S+\s+)*0?777\b|\bchmod\s+(?:-\S+\s+)*a?\+rwx\b")),
    ("chown", re.compile(_CMD + r"chown\s")),
    ("fork bomb", re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:")),
    ("raw device write", re.compile(r">\s*/dev/(?:sd|nvme|hd|vd|mmcblk)")),
    ("kill all", re.compile(_CMD + r"(?:killall|pkill)\s|\bkill\s+-9\s+-1\b")),
    ("system services", re.compile(_CMD + r"(?:crontab|systemctl|service|launchctl)\s")),
    ("home/env expansion", re.compile(r"\$\{?(?:HOME|OLDPWD)\b")),
    ("eval/decoded exec", re.compile(_CMD + r"eval\s|base64\s+(?:-d|--decode)[^\n]*\|\s*(?:ba|z)?sh\b")),
]


def _recursive_rm(command: str) -> bool:
    """rm with -r/-R/--recursive (with or without -f)."""
    for m in re.finditer(_CMD + r"rm\s+([^;&|\n]*)", command):
        args = m.group(1).split()
        for a in args:
            if a == "--":
                break
            if a in ("--recursive",) or (a.startswith("-") and not a.startswith("--")
                                         and ("r" in a or "R" in a)):
                return True
    return False


SAFE_OUTSIDE = {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/stdin", "/dev/zero",
                "/dev/urandom", "/dev/random", "/dev/tty"}
_REDIRECT = re.compile(r"^\d*[<>]+&?")


def _path_candidates(command: str) -> list[str] | None:
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return None  # unbalanced quotes etc. -> treat as risky
    out = []
    for tok in tokens:
        tok = _REDIRECT.sub("", tok)
        for part in [tok] + tok.split("=")[1:]:
            if part.startswith(("/", "~")) or part == ".." or part.startswith("../") \
                    or "/../" in part or part.endswith("/.."):
                out.append(part)
    return out


def _cd_home(command: str) -> bool:
    return bool(re.search(r"(?:^|[;&|(]\s*)cd\s*(?:$|[;&|)])", command.strip()))


def danger_reason(command: str, workdir: Path) -> str | None:
    """Return why a command needs approval, or None if it may run unattended."""
    if _recursive_rm(command):
        return "matches dangerous pattern: rm -rf"
    for name, rx in DANGEROUS_PATTERNS:
        if rx.search(command):
            return f"matches dangerous pattern: {name}"
    if _cd_home(command):
        return "changes directory outside WORKDIR"
    candidates = _path_candidates(command)
    if candidates is None:
        return "could not parse command safely"
    for c in candidates:
        if c in SAFE_OUTSIDE:
            continue
        try:
            resolve_in_workdir(workdir, c)
        except PathError:
            return f"touches a path outside WORKDIR: {c}"
    return None
