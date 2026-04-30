"""
agenc — tool definitions and handlers.

This module defines the OpenAI function-calling schema and implements
handlers for all tools. No shell execution — each tool is a specific,
safe operation.
"""

import difflib
import re
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from config import AUTO_WRITE, CWD, MAX_FILE_BYTES, MAX_READ_LINES

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
            "name": "list_files",
            "description": (
                "List files and directories. "
                "Use `path` to specify a directory (default: '.'). "
                "Set `recursive=True` to list all files recursively. "
                "Set `all=True` to include hidden files (dotfiles)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path relative to working directory. Default: '.'",
                        "default": ".",
                    },
                    "all": {
                        "type": "boolean",
                        "description": "Include hidden files (dotfiles). Default: false",
                        "default": False,
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "List recursively. Default: false",
                        "default": False,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": (
                "Search for files by name pattern (glob-style). "
                "Use `pattern` like '*.py' or 'test_*'. "
                "Use `path` to specify the root directory (default: '.')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Root directory for search. Default: '.'",
                        "default": ".",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern to match file names, e.g. '*.py', 'test_*'",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_text",
            "description": (
                "Search for text within file contents. "
                "Returns matching lines with file paths and line numbers. "
                "Use `pattern` for the text/regex to search. "
                "Use `include` to filter by file pattern (e.g. '*.py'). "
                "Use `path` to specify root directory (default: '.')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Root directory for search. Default: '.'",
                        "default": ".",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "Text or regex pattern to search for",
                    },
                    "include": {
                        "type": "string",
                        "description": "File glob pattern to filter which files to search, e.g. '*.py'",
                        "default": "*",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read file contents. "
                "Use `path` for the file path. "
                "Use `offset` to start reading from a specific line (1-indexed, default: 1). "
                "Use `limit` to restrict the number of lines read (default: 2000). "
                "Files larger than 1MB are rejected."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path relative to working directory",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Starting line number (1-indexed). Default: 1",
                        "default": 1,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum lines to read. Default: 2000",
                        "default": MAX_READ_LINES,
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_file",
            "description": (
                "Create a new file or overwrite an existing one. "
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
                "Always read the file first with read_file to get the exact text to replace. "
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
# Path validation helpers
# ---------------------------------------------------------------------------


def validate_file_path(path_str: str) -> tuple[Optional[Path], Optional[str]]:
    """Validate and resolve a file path. Returns (resolved_path, error_msg)."""
    if not path_str or not path_str.strip():
        return None, "Path is empty."
    try:
        resolved = (CWD / path_str).resolve()
    except (OSError, ValueError) as exc:
        return None, f"Invalid path: {exc}"
    # Check path is within CWD
    try:
        resolved.relative_to(CWD)
    except ValueError:
        return None, f"Path '{path_str}' resolves outside the working directory."
    return resolved, None


def validate_dir_path(path_str: str) -> tuple[Optional[Path], Optional[str]]:
    """Validate and resolve a directory path. Returns (resolved_path, error_msg)."""
    if not path_str or not path_str.strip():
        return None, "Path is empty."
    try:
        resolved = (CWD / path_str).resolve()
    except (OSError, ValueError) as exc:
        return None, f"Invalid path: {exc}"
    try:
        resolved.relative_to(CWD)
    except ValueError:
        return None, f"Path '{path_str}' resolves outside the working directory."
    if not resolved.is_dir():
        return None, f"Not a directory: {path_str}"
    return resolved, None


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


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
    """Ask the user to confirm a file write. Returns True if approved."""
    if AUTO_WRITE:
        console.print("  [info]auto-approved (AGENC_AUTO_WRITE=1)[/info]")
        return True
    try:
        response = console.input(f"  {prompt_text} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        console.print()
        return False
    return response in ("y", "yes")


def highlight_word_diff(old_line: str, new_line: str) -> tuple[Text, Text]:
    """Compare two lines and return Rich Text with word-level highlighting."""
    if old_line == new_line:
        return Text(old_line), Text(new_line)
    
    matcher = difflib.SequenceMatcher(None, old_line, new_line)
    old_result = Text()
    new_result = Text()
    
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            old_result.append(old_line[i1:i2])
            new_result.append(new_line[j1:j2])
        elif tag == 'delete':
            old_result.append(old_line[i1:i2], style="strike red")
        elif tag == 'insert':
            new_result.append(new_line[j1:j2], style="bold green")
        elif tag == 'replace':
            old_result.append(old_line[i1:i2], style="strike red")
            new_result.append(new_line[j1:j2], style="bold green")
    
    return old_result, new_result


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


def handle_list_files(args: dict) -> str:
    """Handle the list_files tool call. Returns a formatted directory listing."""
    path_str = args.get("path", ".")
    show_all = args.get("all", False)
    recursive = args.get("recursive", False)

    resolved, error = validate_dir_path(path_str)
    if error:
        return f"Error: {error}"

    try:
        if recursive:
            entries = sorted(resolved.rglob("*"))
        else:
            entries = sorted(resolved.iterdir())
    except PermissionError as exc:
        return f"Error: Permission denied: {exc}"

    if not show_all:
        entries = [e for e in entries if not e.name.startswith(".")]

    lines = []
    for entry in entries:
        try:
            rel = entry.relative_to(CWD)
        except ValueError:
            rel = entry
        prefix = "📁 " if entry.is_dir() else "📄 "
        lines.append(f"{prefix}{rel}")

    if not lines:
        return "(empty directory)"

    return "\n".join(lines)


def handle_search_files(args: dict) -> str:
    """Handle the search_files tool call. Returns matching file paths."""
    pattern = args.get("pattern", "")
    path_str = args.get("path", ".")

    if not pattern:
        return "Error: pattern is required."

    resolved, error = validate_dir_path(path_str)
    if error:
        return f"Error: {error}"

    try:
        matches = sorted(resolved.rglob(pattern))
    except Exception as exc:
        return f"Error: {exc}"

    lines = []
    for entry in matches:
        if entry.is_file():
            try:
                rel = entry.relative_to(CWD)
            except ValueError:
                rel = entry
            lines.append(str(rel))

    if not lines:
        return f"No files matching '{pattern}'"

    return "\n".join(lines)


def handle_search_text(args: dict) -> str:
    """Handle the search_text tool call. Returns matching lines with context."""
    pattern = args.get("pattern", "")
    path_str = args.get("path", ".")
    include = args.get("include", "*")

    if not pattern:
        return "Error: pattern is required."

    resolved, error = validate_dir_path(path_str)
    if error:
        return f"Error: {error}"

    try:
        files = sorted(resolved.rglob(include))
    except Exception as exc:
        return f"Error: {exc}"

    files = [f for f in files if f.is_file()]
    results = []

    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return f"Error: Invalid regex pattern: {exc}"

    for file_path in files:
        try:
            if file_path.stat().st_size > MAX_FILE_BYTES:
                continue
            content = file_path.read_text(encoding="utf-8", errors="ignore")
        except (PermissionError, OSError):
            continue

        for line_num, line in enumerate(content.splitlines(), 1):
            if regex.search(line):
                try:
                    rel = file_path.relative_to(CWD)
                except ValueError:
                    rel = file_path
                results.append(f"{rel}:{line_num}: {line}")

    if not results:
        return f"No matches for '{pattern}'"

    if len(results) > 100:
        results = results[:100] + [f"... ({len(results) - 100} more matches)"]

    return "\n".join(results)


def handle_read_file(args: dict) -> str:
    """Handle the read_file tool call. Returns file contents."""
    path_str = args.get("path", "")
    offset = args.get("offset", 1)
    limit = args.get("limit", MAX_READ_LINES)

    resolved, error = validate_file_path(path_str)
    if error:
        return f"Error: {error}"

    if not resolved.is_file():
        return f"Error: File not found: {path_str}"

    try:
        size = resolved.stat().st_size
        if size > MAX_FILE_BYTES:
            return f"Error: File too large ({size} bytes). Max: {MAX_FILE_BYTES} bytes."
        content = resolved.read_text(encoding="utf-8")
    except Exception as exc:
        return f"Error reading file: {exc}"

    lines = content.splitlines()
    start = max(0, offset - 1)
    end = start + limit
    selected = lines[start:end]
    total_lines = len(lines)

    try:
        rel_path = resolved.relative_to(CWD)
    except ValueError:
        rel_path = resolved

    header = f"📄 {rel_path} (lines {start + 1}-{min(end, total_lines)} of {total_lines})\n"
    return header + "\n".join(selected)


def handle_create_file(args: dict) -> str:
    """Handle the create_file tool call. Returns result message."""
    path_str = args.get("path", "")
    content = args.get("content", "")

    resolved, error = validate_file_path(path_str)
    if error:
        return f"Error: {error}"

    exists = resolved.is_file()
    lines = content.splitlines()
    lexer = guess_lexer(resolved)

    action = "overwrite" if exists else "create"
    console.print(
        f"  [tool]▸ {action}: {resolved.relative_to(CWD)} ({len(lines)} lines)[/tool]"
    )

    if len(lines) <= 40:
        console.print(Syntax(content, lexer, theme="monokai", line_numbers=True))
    else:
        preview = "\n".join(lines[:20]) + f"\n\n... [{len(lines) - 40} lines omitted] ...\n\n" + "\n".join(lines[-10:])
        console.print(Syntax(preview, lexer, theme="monokai", line_numbers=True))

    if exists:
        console.print(f"  [warning]File exists and will be overwritten.[/warning]")

    if not confirm_write(f"[bold]Apply {action}? [y/n]:[/bold]"):
        return "User rejected the file write."

    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        return f"File {'overwritten' if exists else 'created'}: {resolved.relative_to(CWD)} ({len(lines)} lines)"
    except Exception as exc:
        return f"Error writing file: {exc}"


def handle_edit_file(args: dict) -> str:
    """Handle the edit_file tool call. Returns result message."""
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
        return "Error: old_str not found in file. Read the file first."
    if count > 1:
        return f"Error: old_str appears {count} times. Make it more specific."

    before_match = original[: original.index(old_str)]
    start_line = before_match.count("\n") + 1
    old_lines = old_str.splitlines()
    new_lines = new_str.splitlines()

    rel_path = resolved.relative_to(CWD)
    console.print(f"  [tool]▸ edit: {rel_path} (line {start_line})[/tool]")

    SIDE_BY_SIDE_MIN_WIDTH = 100
    SIDE_BY_SIDE_MAX_LINES = 30
    use_side_by_side = (
        console.width >= SIDE_BY_SIDE_MIN_WIDTH
        and max(len(old_lines), len(new_lines)) <= SIDE_BY_SIDE_MAX_LINES
    )

    if use_side_by_side:
        col_width = (console.width - 12) // 2
        table = Table(show_header=True, box=None, padding=(0, 1))
        table.add_column("before", width=col_width, no_wrap=True)
        table.add_column("after", width=col_width, no_wrap=True)
        max_len = max(len(old_lines), len(new_lines))
        for i in range(max_len):
            ol = old_lines[i] if i < len(old_lines) else ""
            nl = new_lines[i] if i < len(new_lines) else ""
            ot, nt = highlight_word_diff(ol, nl)
            table.add_row(ot, nt)
        console.print(table)
    else:
        max_len = max(len(old_lines), len(new_lines), 1)
        for i in range(max_len):
            ol = old_lines[i] if i < len(old_lines) else ""
            nl = new_lines[i] if i < len(new_lines) else ""
            if ol == nl:
                console.print(f"  {ol}")
            else:
                ot, nt = highlight_word_diff(ol, nl)
                if ol:
                    console.print(Text.assemble(("  - ", "red"), ot))
                if nl:
                    console.print(Text.assemble(("  + ", "green"), nt))

    if not confirm_write("[bold]Apply edit? [y/n]:[/bold]"):
        return "User rejected the edit."

    try:
        new_content = original.replace(old_str, new_str, 1)
        resolved.write_text(new_content, encoding="utf-8")
        return f"Edited {rel_path}: {len(old_lines)} lines → {len(new_lines)} lines at line {start_line}."
    except Exception as exc:
        return f"Error writing file: {exc}"


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


def display_tool_call(name: str, args: dict):
    """Display a tool call being made."""
    if name == "list_files":
        path = args.get("path", ".")
        recursive = args.get("recursive", False)
        suffix = " (recursive)" if recursive else ""
        console.print(f"  [tool]▸ list_files: {path}{suffix}[/tool]")
    elif name == "search_files":
        pattern = args.get("pattern", "")
        path = args.get("path", ".")
        console.print(f"  [tool]▸ search_files: {path} '**/{pattern}'[/tool]")
    elif name == "search_text":
        pattern = args.get("pattern", "")
        path = args.get("path", ".")
        include = args.get("include", "*")
        console.print(f"  [tool]▸ search_text: {path} '**/{include}' grep '{pattern}'[/tool]")
    elif name == "read_file":
        path = args.get("path", "")
        offset = args.get("offset", 1)
        limit = args.get("limit", MAX_READ_LINES)
        console.print(f"  [tool]▸ read_file: {path} (lines {offset}-{offset + limit - 1})[/tool]")
    # create_file and edit_file handle their own display
