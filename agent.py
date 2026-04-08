#!/usr/bin/env python3
"""
agenc — a code review agent that connects to any OpenAI-compatible endpoint.

It explores your repo with sandboxed shell commands and gives you feedback on your code.
"""

import json
import os
import re
import readline  # noqa: F401  — importing enables arrow-key history in input()
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Optional

from openai import OpenAI
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.theme import Theme

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_MODEL = os.environ.get("AGENC_MODEL", "gpt-4o")
DEFAULT_BASE_URL = os.environ.get("AGENC_BASE_URL", "https://api.openai.com/v1")
API_KEY = os.environ.get("AGENC_API_KEY", os.environ.get("OPENAI_API_KEY", ""))

# Commands the agent is allowed to run (read-only utilities).
ALLOWED_COMMANDS = frozenset(
    {
        "ls",
        "cat",
        "head",
        "tail",
        "grep",
        "egrep",
        "find",
        "wc",
        "file",
        "tree",
        "diff",
        "stat",
        "du",
        "sort",
        "uniq",
        "awk",
        "sed",  # sed without -i is read-only
        "cut",
        "tr",
        "basename",
        "dirname",
        "realpath",
        "echo",
        "pwd",
        "bat",       # pretty cat
        "rg",        # ripgrep
        "fd",        # fd-find
        "tokei",     # code stats
        "cloc",      # code stats
        "hexdump",
        "xxd",
        "md5sum",
        "sha256sum",
    }
)

# Patterns that should never appear in a command — even piped.
# Uses word boundaries (\b) to avoid matching inside longer words (e.g. "stash" matching "sh").
BLOCKED_PATTERNS = re.compile(
    r"""
    ( \brm\s | \brmdir\b | \bmv\s | \bcp\s | \bchmod\b | \bchown\b | \bchgrp\b | \bmkfs\b | \bdd\s
    | >\s | >>  | \btee\s
    | \bcurl\b | \bwget\b | \bnc\s | \bncat\b | \bsocat\b
    | \bpython\b | \bpython3\b | \bperl\b | \bruby\b | \bnode\b | \bbash\b | \bsh\s | \bzsh\b
    | \bsudo\b | \bsu\s
    | \bkill\b | \bpkill\b | \breboot\b | \bshutdown\b
    | \bapt\b | \byum\b | \bdnf\b | \bpacman\b | \bbrew\b
    | \bdocker\b | \bkubectl\b
    | \bssh\b | \bscp\b | \brsync\b
    | \beval\b | \bexec\b
    | \bsed\s+-i
    | \bxargs\b
    | \$\(
    | `
    )
    """,
    re.VERBOSE,
)

MAX_OUTPUT_BYTES = 100_000  # truncate huge command outputs

# ---------------------------------------------------------------------------
# Git subcommand validation
# ---------------------------------------------------------------------------

# Git subcommands that are safe for reading repo state + making commits.
ALLOWED_GIT_SUBCOMMANDS = frozenset(
    {
        # Reading state
        "status",
        "diff",
        "log",
        "show",
        "blame",
        "shortlog",
        "describe",
        "branch",       # listing branches (--delete is blocked below)
        "tag",          # listing tags (--delete is blocked below)
        "stash",        # stash list only (other stash ops blocked below)
        "ls-files",
        "ls-tree",
        "rev-parse",
        "rev-list",
        "cat-file",
        "name-rev",
        "reflog",
        # Making commits
        "add",
        "commit",
    }
)

# Git flags/arguments that should never appear — even on allowed subcommands.
BLOCKED_GIT_PATTERNS = re.compile(
    r"""
    ( --force
    | --hard
    | --delete
    | -[dD]\b          # branch -d / -D
    | --mirror
    | --bare
    | --no-verify      # skip commit hooks — suspicious
    )
    """,
    re.VERBOSE,
)


def validate_git_command(parts: list[str]) -> Optional[str]:
    """Validate a git command.  `parts` is the shlex-split command starting with 'git'.
    Returns an error message if blocked, else None.
    """
    # Strip global git flags to find the subcommand (e.g. git -C /foo status)
    args = parts[1:]
    subcmd = None
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("-"):
            # Skip flags that take a value: -C, -c, --git-dir, --work-tree
            if a in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
                i += 2  # skip flag + its value
            else:
                i += 1
        else:
            subcmd = a
            break

    if subcmd is None:
        return "Blocked: git command with no subcommand."

    if subcmd not in ALLOWED_GIT_SUBCOMMANDS:
        return f"Blocked: git subcommand '{subcmd}' is not in the allow-list. Allowed: {', '.join(sorted(ALLOWED_GIT_SUBCOMMANDS))}"

    # Check for dangerous flags on the full argument string
    full_args = " ".join(parts[1:])
    if BLOCKED_GIT_PATTERNS.search(full_args):
        return "Blocked: git command contains a forbidden flag."

    # Special case: 'stash' is only allowed for listing
    if subcmd == "stash":
        stash_action = args[i + 1] if i + 1 < len(args) else "list"
        if stash_action not in ("list", "show"):
            return f"Blocked: 'git stash {stash_action}' is not allowed. Only 'git stash list' and 'git stash show'."

    return None

# ---------------------------------------------------------------------------
# .gitignore-aware tree
# ---------------------------------------------------------------------------


def load_gitignore_patterns(root: Path) -> list[str]:
    """Load .gitignore patterns from the repo root.  Returns raw pattern strings."""
    gitignore = root / ".gitignore"
    if not gitignore.is_file():
        return []
    patterns = []
    for line in gitignore.read_text(errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.append(line)
    return patterns


def matches_gitignore(path: Path, root: Path, patterns: list[str]) -> bool:
    """Very simple gitignore matching — covers the common cases."""
    rel = str(path.relative_to(root))
    name = path.name
    for pat in patterns:
        # directory-only patterns (trailing /)
        check = pat.rstrip("/")
        if check in (rel, name):
            return True
        # simple wildcard
        if "*" in check:
            import fnmatch

            if fnmatch.fnmatch(name, check) or fnmatch.fnmatch(rel, check):
                return True
    return False


# ---------------------------------------------------------------------------
# Sandbox: validate + run shell commands
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
            if not str(resolved).startswith(str(CWD)):
                return f"Path '{arg}' resolves outside the working directory."
    return None


def compress_output(stdout: str, stderr: str, returncode: int) -> tuple[str, str]:
    """Compress command output intelligently based on exit code.

    Success (rc=0): the model usually just needs confirmation + a glimpse.
    Failure (rc!=0): the model needs enough context to diagnose the problem.
    """
    lines = stdout.splitlines()
    num_lines = len(lines)

    if returncode == 0:
        # Happy path — aggressively compress
        if num_lines <= 60:
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
        # On failure, always preserve full stderr
        # (no truncation on stderr for errors)

    # Final byte-level safety net
    if len(stdout) > MAX_OUTPUT_BYTES:
        stdout = stdout[-MAX_OUTPUT_BYTES:] + "\n... [byte-truncated]"
    if len(stderr) > MAX_OUTPUT_BYTES:
        stderr = stderr[-MAX_OUTPUT_BYTES:]

    return stdout, stderr


def run_shell(cmd_str: str) -> dict:
    """Validate and execute a read-only shell command.  Returns {stdout, stderr, returncode}."""
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
        stdout, stderr = compress_output(proc.stdout, proc.stderr, proc.returncode)
        return {"stdout": stdout, "stderr": stderr, "returncode": proc.returncode}
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": "Command timed out (30s limit).", "returncode": 1}
    except Exception as exc:
        return {"stdout": "", "stderr": str(exc), "returncode": 1}


# ---------------------------------------------------------------------------
# Tool definitions (OpenAI function-calling schema)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Run a shell command to explore and interact with the repository. "
                "Read commands: ls, cat, head, tail, grep, find, wc, tree, diff, rg, fd, etc. "
                "Git commands: git status, git diff, git log, git show, git blame, git add, git commit, etc. "
                "Destructive git operations (reset, push, rebase, cherry-pick, merge, checkout, etc.) are blocked. "
                "All paths must stay within the current working directory."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute (read-only).",
                    }
                },
                "required": ["command"],
            },
        },
    }
]

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = f"""\
You are **agenc**, a coding assistant running inside a developer's terminal.

## Your role
- Review code, explain logic, find bugs, suggest improvements, and answer questions about the repo.
- You can explore the repo using the `bash` tool.
- You can use git to inspect repo state and make commits.

## Working directory
{CWD}

## How to work
1. When the user asks about code, use `bash` to read the relevant files first.  Don't guess at contents.
2. Be specific: reference file names, line numbers, function names.
3. Give actionable feedback — concrete suggestions the developer can apply.
4. When reviewing, look for: bugs, edge cases, naming, structure, performance, security, readability.
5. Keep responses focused and useful. Use markdown for formatting.
6. Command output is automatically compressed — on success you may see only the tail of long outputs.  \
If you need specific lines from the middle, use head/tail/sed to extract them.

## Git
- You can read repo state: `git status`, `git diff`, `git log`, `git show`, `git blame`, `git branch`, etc.
- You can stage and commit: `git add`, `git commit`.
- Destructive operations are blocked: no reset, push, rebase, cherry-pick, merge, checkout, clean, etc.
- Write clear, conventional commit messages.

## Constraints
- Shell commands: read-only utilities (ls, cat, grep, find, head, tail, tree, rg, etc.) + git.
- All paths must stay within the working directory.
- If you need information, explore with bash — don't assume.
"""

# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

console = Console(
    theme=Theme(
        {
            "info": "dim cyan",
            "warning": "yellow",
            "tool": "dim green",
        }
    )
)


def make_client() -> OpenAI:
    if not API_KEY:
        console.print(
            "[warning]Warning: No API key found. Set AGENC_API_KEY or OPENAI_API_KEY.[/warning]"
        )
    return OpenAI(base_url=DEFAULT_BASE_URL, api_key=API_KEY or "unused")


def display_tool_call(name: str, args: dict):
    cmd = args.get("command", "")
    console.print(f"  [tool]▸ {cmd}[/tool]")


def display_tool_result(result: dict):
    rc = result["returncode"]
    out = result["stdout"].strip()
    err = result["stderr"].strip()
    if err:
        console.print(f"  [warning]stderr: {err[:500]}[/warning]")
    if out:
        # Show a short preview if long
        lines = out.splitlines()
        if len(lines) > 30:
            preview = "\n".join(lines[:15]) + f"\n... ({len(lines)} lines total)"
            console.print(Syntax(preview, "text", theme="monokai", line_numbers=False))
        else:
            console.print(Syntax(out, "text", theme="monokai", line_numbers=False))
    if rc != 0 and not err:
        console.print(f"  [warning]exit code {rc}[/warning]")


def chat_turn(client: OpenAI, messages: list, model: str) -> str:
    """Run one turn of the agentic loop: call the model, execute any tool calls, repeat."""
    while True:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
        )

        msg = response.choices[0].message

        # Append the assistant message to history
        messages.append(msg.model_dump(exclude_none=True))

        # If no tool calls, we're done — return the text
        if not msg.tool_calls:
            return msg.content or ""

        # Process each tool call
        for tc in msg.tool_calls:
            fn = tc.function
            try:
                args = json.loads(fn.arguments)
            except json.JSONDecodeError:
                args = {"command": fn.arguments}

            display_tool_call(fn.name, args)

            if fn.name == "bash":
                result = run_shell(args.get("command", ""))
            else:
                result = {
                    "stdout": "",
                    "stderr": f"Unknown tool: {fn.name}",
                    "returncode": 1,
                }

            display_tool_result(result)

            # Feed tool result back into conversation
            tool_output = ""
            if result["stdout"]:
                tool_output += result["stdout"]
            if result["stderr"]:
                tool_output += f"\nSTDERR: {result['stderr']}"
            if not tool_output.strip():
                tool_output = "(empty output)"

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_output,
                }
            )


def print_welcome():
    console.print(
        Panel(
            f"[bold]agenc[/bold] — code review agent\n"
            f"model: [info]{DEFAULT_MODEL}[/info]  endpoint: [info]{DEFAULT_BASE_URL}[/info]\n"
            f"cwd: [info]{CWD}[/info]\n\n"
            f"Type your question or [bold]/help[/bold] for commands. [bold]/quit[/bold] to exit.",
            border_style="blue",
        )
    )


def print_help():
    console.print(
        Markdown(
            """\
### Commands
- `/help`  — show this message
- `/quit`  — exit
- `/clear` — clear conversation history
- `/model <name>` — switch model
- `/cwd`   — print working directory
"""
        )
    )


def main():
    global DEFAULT_MODEL

    print_welcome()
    client = make_client()

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    while True:
        try:
            user_input = console.input("[bold blue]you >[/bold blue] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[info]Goodbye![/info]")
            break

        if not user_input:
            continue

        # Slash commands
        if user_input.startswith("/"):
            cmd_parts = user_input.split(maxsplit=1)
            cmd = cmd_parts[0].lower()

            if cmd in ("/quit", "/exit", "/q"):
                console.print("[info]Goodbye![/info]")
                break
            elif cmd == "/help":
                print_help()
                continue
            elif cmd == "/clear":
                messages = [{"role": "system", "content": SYSTEM_PROMPT}]
                console.print("[info]Conversation cleared.[/info]")
                continue
            elif cmd == "/model":
                if len(cmd_parts) > 1:
                    DEFAULT_MODEL = cmd_parts[1]
                    console.print(f"[info]Model set to {DEFAULT_MODEL}[/info]")
                else:
                    console.print(f"[info]Current model: {DEFAULT_MODEL}[/info]")
                continue
            elif cmd == "/cwd":
                console.print(f"[info]{CWD}[/info]")
                continue
            else:
                console.print(f"[warning]Unknown command: {cmd}[/warning]")
                continue

        messages.append({"role": "user", "content": user_input})

        try:
            console.print()  # breathing room
            reply = chat_turn(client, messages, DEFAULT_MODEL)
            console.print()
            console.print(Markdown(reply))
            console.print()
        except KeyboardInterrupt:
            console.print("\n[warning]Interrupted.[/warning]")
            # Remove the dangling user message if the model didn't reply
            if messages[-1]["role"] == "user":
                messages.pop()
        except Exception as exc:
            console.print(f"\n[bold red]Error:[/bold red] {exc}")
            # Remove the user message so we don't get stuck
            if messages[-1]["role"] == "user":
                messages.pop()


if __name__ == "__main__":
    main()
