#!/usr/bin/env python3
"""
agenc — a code review agent that connects to any OpenAI-compatible endpoint.

It explores your repo with sandboxed shell commands and gives you feedback on your code.
"""

import json
import readline  # noqa: F401  — importing enables arrow-key history in input()
from pathlib import Path

from openai import OpenAI
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.theme import Theme

from config import API_KEY, DEFAULT_BASE_URL, DEFAULT_MODEL, CWD
from tools import (
    TOOLS,
    display_tool_call,
    display_tool_result,
    handle_bash,
    handle_create_file,
    handle_edit_file,
)

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = f"""\
You are **agenc**, a coding assistant running inside a developer's terminal.

## Your role
- Review code, explain logic, find bugs, suggest improvements, and answer questions about the repo.
- You can explore the repo using the `bash` tool.
- You can create and edit files using `create_file` and `edit_file`.
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

## File editing
- Use `edit_file` for surgical changes — always read the file first to get exact text for old_str.
- Use `create_file` for new files or full rewrites.
- The user must approve every write — if they reject, adjust your approach.
- Make focused, minimal edits. Don't rewrite entire files when a small edit will do.
- After editing, consider using `git diff` to verify the change looks correct.

## Git
- You can read repo state: `git status`, `git diff`, `git log`, `git show`, `git blame`, `git branch`, etc.
- You can stage and commit: `git add`, `git commit`.
- Destructive operations are blocked: no reset, push, rebase, cherry-pick, merge, checkout, clean, etc.
- Write clear, conventional commit messages.

## Constraints
- Shell commands: read-only utilities (ls, cat, grep, find, head, tail, tree, rg, etc.) + git.
- All paths must stay within the working directory.
- If you need information, explore with bash — don't assume.
- Only make the changes you were asked to make. Do not modify behaviour, expand permissions, \
or refactor anything beyond the scope of the request.
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
                _, result = handle_bash(args)
                display_tool_result(result)
                # Build tool output string
                tool_output = ""
                if result["stdout"]:
                    tool_output += result["stdout"]
                if result["stderr"]:
                    tool_output += f"\nSTDERR: {result['stderr']}"
                if not tool_output.strip():
                    tool_output = "(empty output)"
            elif fn.name == "create_file":
                tool_output = handle_create_file(args)
                console.print(f"  [info]{tool_output}[/info]")
            elif fn.name == "edit_file":
                tool_output = handle_edit_file(args)
                console.print(f"  [info]{tool_output}[/info]")
            else:
                tool_output = f"Unknown tool: {fn.name}"

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
- `/paste` — enter multi-line input mode (end with three double-quotes on its own line)
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
            elif cmd == "/paste":
                sentinel = '"""'
                console.print(f'[info]Multi-line mode — paste your text, then type {sentinel} on its own line to send.[/info]')
                lines = []
                while True:
                    try:
                        line = console.input("")
                    except (EOFError, KeyboardInterrupt):
                        break
                    if line.strip() == sentinel:
                        break
                    lines.append(line)
                user_input = "\n".join(lines).strip()
                if not user_input:
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
