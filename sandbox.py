"""
agenc — sandbox for safe command execution.

This module validates shell commands, checks paths stay within CWD,
and executes commands with output compression.
"""

import shlex
import subprocess
from pathlib import Path
from typing import Optional

from config import (
    ALLOWED_COMMANDS,
    BLOCKED_PATTERNS,
    CONTENT_COMMANDS,
    CONTENT_GIT_SUBCOMMANDS,
    MAX_OUTPUT_BYTES,
)
from git import validate_git_command

# ---------------------------------------------------------------------------
# Working directory jail
# ---------------------------------------------------------------------------

CWD = Path.cwd().resolve()


def validate_command(cmd_str: str) -> Optional[str]:
    """Return an error message if the command is not allowed, else None."""
    # Block dangerous patterns anywhere in the string
    if BLOCKED_PATTERNS.search(cmd_str):
        return f"Blocked: command contains a forbidden pattern."

    # Split on pipes and validate each segment
    segments = cmd_str.split("|")
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        try:
            parts = shlex.split(seg)
        except ValueError as exc:
            return f"Could not parse command: {exc}"
        if not parts:
            continue
        base_cmd = Path(parts[0]).name  # handle /usr/bin/ls etc.

        # Git gets its own subcommand-level validation
        if base_cmd == "git":
            error = validate_git_command(parts)
            if error:
                return error
        elif base_cmd not in ALLOWED_COMMANDS:
            return f"Command '{base_cmd}' is not in the allow-list."

    return None


def resolve_and_check_paths(cmd_str: str) -> Optional[str]:
    """Ensure all file/directory arguments resolve inside CWD."""
    segments = cmd_str.split("|")
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        try:
            parts = shlex.split(seg)
        except ValueError:
            continue
        for arg in parts[1:]:
            if arg.startswith("-"):
                continue
            # Try to resolve the path argument
            try:
                resolved = (CWD / arg).resolve()
            except (OSError, ValueError):
                continue
            if not str(resolved).startswith(str(CWD) + "/") and resolved != CWD:
                return f"Path '{arg}' resolves outside the working directory."
    return None


def is_content_command(cmd_str: str) -> bool:
    """Return True if the first command in the pipeline produces content output."""
    first_segment = cmd_str.split("|")[0].strip()
    try:
        parts = shlex.split(first_segment)
    except ValueError:
        return False
    if not parts:
        return False

    base_cmd = Path(parts[0]).name

    if base_cmd in CONTENT_COMMANDS:
        return True

    if base_cmd == "git":
        # Find the subcommand (skip flags)
        for arg in parts[1:]:
            if not arg.startswith("-"):
                return arg in CONTENT_GIT_SUBCOMMANDS
    return False


def compress_output(
    stdout: str, stderr: str, returncode: int, *, content: bool = False
) -> tuple[str, str]:
    """Compress command output intelligently based on exit code.

    Success (rc=0): the model usually just needs confirmation + a glimpse.
    Failure (rc!=0): the model needs enough context to diagnose the problem.

    If content=True, skip compression on success (the output IS the answer).
    """
    lines = stdout.splitlines()
    num_lines = len(lines)

    if returncode == 0:
        if content:
            pass  # never compress content commands on success
        elif num_lines <= 60:
            pass  # short enough, keep as-is
        elif num_lines <= 200:
            # Medium output: first 10 + last 20 lines
            stdout = (
                "\n".join(lines[:10])
                + f"\n\n... [{num_lines - 30} lines omitted — command succeeded] ...\n\n"
                + "\n".join(lines[-20:])
            )
        else:
            # Large output: just the tail + a count
            stdout = (
                f"[OK — {num_lines} lines of output, showing last 30]\n"
                + "\n".join(lines[-30:])
            )
    else:
        # Failure — keep more context, especially the end where errors land
        if num_lines <= 120:
            pass  # keep all
        else:
            stdout = (
                f"[FAILED — {num_lines} lines total, showing last 80]\n"
                + "\n".join(lines[-80:])
            )
        # On failure, preserve stderr (subject to the 100KB safety net below)

    # Final byte-level safety net
    if len(stdout) > MAX_OUTPUT_BYTES:
        stdout = stdout[-MAX_OUTPUT_BYTES:] + "\n... [byte-truncated]"
    if len(stderr) > MAX_OUTPUT_BYTES:
        stderr = stderr[-MAX_OUTPUT_BYTES:]

    return stdout, stderr


def run_shell(cmd_str: str) -> dict:
    """Validate and execute a shell command.  Returns {stdout, stderr, returncode}."""
    error = validate_command(cmd_str)
    if error:
        return {"stdout": "", "stderr": error, "returncode": 1}

    error = resolve_and_check_paths(cmd_str)
    if error:
        return {"stdout": "", "stderr": error, "returncode": 1}

    try:
        proc = subprocess.run(
            cmd_str,
            shell=True,
            cwd=str(CWD),
            capture_output=True,
            timeout=30,
            text=True,
        )
        content = is_content_command(cmd_str)
        stdout, stderr = compress_output(
            proc.stdout, proc.stderr, proc.returncode, content=content
        )
        return {"stdout": stdout, "stderr": stderr, "returncode": proc.returncode}
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": "Command timed out (30s limit).", "returncode": 1}
    except Exception as exc:
        return {"stdout": "", "stderr": str(exc), "returncode": 1}
