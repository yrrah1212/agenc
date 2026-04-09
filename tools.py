"""
agenc — tool definitions and handlers.

This module defines the OpenAI function-calling schema and implements
handlers for bash, create_file, and edit_file tools.
"""

from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.syntax import Syntax
from rich.theme import Theme

from config import AUTO_WRITE, CWD
from sandbox import run_shell

# ---------------------------------------------------------------------------
# Console setup
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


# ---------------------------------------------------------------------------
# Tool schema
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
                "All paths must stay within the current working directory. "
                "Do NOT use bash to write files — use create_file or edit_file instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute.",
                    }
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_file",
            "description": (
                "Create a new file or overwrite an existing file. "
                "Use this to write new files from scratch. "
                "The user will be shown the content and asked to approve before writing. "
                "All paths must be within the working directory."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path relative to the working directory.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The full content to write to the file.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Edit an existing file by replacing a specific string with new content. "
                "old_str must match exactly one location in the file. "
                "Always read the file first with bash (cat) to get the exact text to replace. "
                "The user will be shown a diff preview and asked to approve. "
                "For large changes, prefer create_file to rewrite the whole file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path relative to the working directory.",
                    },
                    "old_str": {
                        "type": "string",
                        "description": "The exact string to find and replace. Must appear exactly once in the file.",
                    },
                    "new_str": {
                        "type": "string",
                        "description": "The replacement string. Use empty string to delete.",
                    },
                },
                "required": ["path", "old_str", "new_str"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def validate_file_path(path_str: str) -> tuple[Optional[Path], Optional[str]]:
    """Validate and resolve a file path.  Returns (resolved_path, error_msg)."""
    if not path_str or not path_str.strip():
        return None, "Path is empty."
    try:
        resolved = (CWD / path_str).resolve()
    except (OSError, ValueError) as exc:
        return None, f"Invalid path: {exc}"
    if not str(resolved).startswith(str(CWD) + "/") and resolved != CWD:
        return None, f"Path '{path_str}' resolves outside the working directory."
    return resolved, None


def guess_lexer(path: Path) -> str:
    """Guess a Pygments lexer name from a file extension."""
    ext_map = {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".jsx": "jsx", ".tsx": "tsx", ".rs": "rust", ".go": "go",
        ".rb": "ruby", ".java": "java", ".c": "c", ".cpp": "cpp",
        ".h": "c", ".hpp": "cpp", ".cs": "csharp", ".swift": "swift",
        ".kt": "kotlin", ".sh": "bash", ".bash": "bash", ".zsh": "zsh",
        ".html": "html", ".css": "css", ".scss": "scss",
        ".json": "json", ".yaml": "yaml", ".yml": "yaml",
        ".toml": "toml", ".xml": "xml", ".md": "markdown",
        ".sql": "sql", ".lua": "lua", ".r": "r",
        ".dockerfile": "docker", ".tf": "terraform",
        ".ex": "elixir", ".exs": "elixir", ".erl": "erlang",
    }
    return ext_map.get(path.suffix.lower(), "text")


def confirm_write(prompt_text: str) -> bool:
    """Ask the user to confirm a file write.  Returns True if approved."""
    if AUTO_WRITE:
        console.print("  [info]auto-approved (AGENC_AUTO_WRITE=1)[/info]")
        return True
    try:
        response = console.input(f"  {prompt_text} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        console.print()
        return False
    return response in ("y", "yes")


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


def handle_bash(args: dict) -> tuple[str, dict]:
    """Handle the bash tool call.  Returns (display_string, result_dict)."""
    cmd = args.get("command", "")
    result = run_shell(cmd)
    return cmd, result


def handle_create_file(args: dict) -> str:
    """Handle the create_file tool call.  Returns a result string for the model."""
    path_str = args.get("path", "")
    content = args.get("content", "")

    resolved, error = validate_file_path(path_str)
    if error:
        return f"Error: {error}"

    exists = resolved.is_file()
    lines = content.splitlines()
    lexer = guess_lexer(resolved)

    # Display preview
    action = "overwrite" if exists else "create"
    console.print(
        f"  [tool]▸ {action}: {resolved.relative_to(CWD)} "
        f"({len(lines)} lines)[/tool]"
    )

    # Show content preview
    if len(lines) <= 40:
        console.print(Syntax(content, lexer, theme="monokai", line_numbers=True))
    else:
        preview = "\n".join(lines[:20]) + f"\n\n... [{len(lines) - 30} lines] ...\n\n" + "\n".join(lines[-10:])
        console.print(Syntax(preview, lexer, theme="monokai", line_numbers=True))

    if exists:
        console.print(f"  [warning]File exists and will be overwritten.[/warning]")

    if not confirm_write(f"[bold]Apply {action}? [y/n]:[/bold]"):
        return "User rejected the file write."

    # Write the file
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        return f"File {'overwritten' if exists else 'created'}: {resolved.relative_to(CWD)} ({len(lines)} lines)"
    except Exception as exc:
        return f"Error writing file: {exc}"


def handle_edit_file(args: dict) -> str:
    """Handle the edit_file tool call.  Returns a result string for the model."""
    path_str = args.get("path", "")
    old_str = args.get("old_str", "")
    new_str = args.get("new_str", "")

    resolved, error = validate_file_path(path_str)
    if error:
        return f"Error: {error}"

    if not resolved.is_file():
        return f"Error: File not found: {path_str}"

    try:
        original = resolved.read_text(encoding="utf-8")
    except Exception as exc:
        return f"Error reading file: {exc}"

    if not old_str:
        return "Error: old_str is empty."

    count = original.count(old_str)
    if count == 0:
        return "Error: old_str not found in file. Read the file first to get the exact text."
    if count > 1:
        return f"Error: old_str appears {count} times in file. Make it more specific so it matches exactly once."

    # Build diff preview
    rel_path = resolved.relative_to(CWD)
    lexer = guess_lexer(resolved)

    # Find the line range for context
    before_match = original[: original.index(old_str)]
    start_line = before_match.count("\n") + 1
    old_lines = old_str.splitlines()
    new_lines = new_str.splitlines()

    console.print(f"  [tool]▸ edit: {rel_path} (line {start_line})[/tool]")

    # Show diff-style preview
    diff_parts = []
    for line in old_lines:
        diff_parts.append(f"- {line}")
    for line in new_lines:
        diff_parts.append(f"+ {line}")
    if not new_str and old_str:
        diff_parts.append("+ (deleted)")

    diff_text = "\n".join(diff_parts)
    console.print(Syntax(diff_text, "diff", theme="monokai", line_numbers=False))

    if not confirm_write("[bold]Apply edit? [y/n]:[/bold]"):
        return "User rejected the edit."

    # Apply the edit
    try:
        new_content = original.replace(old_str, new_str, 1)
        resolved.write_text(new_content, encoding="utf-8")
        return f"Edited {rel_path}: replaced {len(old_lines)} lines with {len(new_lines)} lines at line {start_line}."
    except Exception as exc:
        return f"Error writing file: {exc}"


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


def display_tool_call(name: str, args: dict):
    """Display a tool call being made."""
    if name == "bash":
        cmd = args.get("command", "")
        console.print(f"  [tool]▸ {cmd}[/tool]")
    # create_file and edit_file handle their own display


def display_tool_result(result: dict):
    """Display the result of a tool call."""
    rc = result.get("returncode", 1)
    out = result.get("stdout", "").strip()
    err = result.get("stderr", "").strip()
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
