#!/usr/bin/env python3
"""
agenc — a code review agent that connects to any OpenAI-compatible endpoint.

It explores your repo with safe, specific tools and gives feedback on your code.
"""

import json
import subprocess
from dataclasses import dataclass

from openai import OpenAI
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.theme import Theme

from config import API_KEY, DEFAULT_BASE_URL, DEFAULT_MODEL, CWD, SHELL_TIMEOUT, SHELL_MAX_LINES, SHELL_MAX_CHARS
from tools import (
    TOOLS,
    display_tool_call,

    handle_list_files,
    handle_search_files,
    handle_search_text,
    handle_read_file,
    handle_create_file,
    handle_edit_file,
)

# ---------------------------------------------------------------------------
# Prompt styling
# ---------------------------------------------------------------------------

PROMPT_STYLE = Style.from_dict({
    "blue": "#0087ff",
    "dim": "#555555",
})


@dataclass
class TokenUsage:
    """Tracks cumulative token usage for a session."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def add(self, prompt: int, completion: int, total: int):
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.total_tokens += total


# ---------------------------------------------------------------------------
# Slash command completion
# ---------------------------------------------------------------------------

SLASH_COMMANDS = {
    "/help": [],
    "/quit": ["/exit", "/q"],
    "/clear": [],
    "/model": [],
    "/models": [],
    "/tokens": ["/usage"],
    "/onboard": [],
    "/shell": [],
}


def get_slash_command_aliases():
    """Return a flat dict mapping all command names to their primary name."""
    aliases = {}
    for primary, alias_list in SLASH_COMMANDS.items():
        aliases[primary] = primary
        for alias in alias_list:
            aliases[alias] = primary
    return aliases


class SlashCommandCompleter(Completer):
    """Provide autocompletion for slash commands and model names."""

    def __init__(self, client=None):
        self.client = client
        self._model_cache = None

    def _get_models(self):
        """Fetch and cache available model names."""
        if self._model_cache is not None:
            return self._model_cache
        if self.client is None:
            return []
        try:
            models = self.client.models.list()
            self._model_cache = [m.id for m in models.data]
            return self._model_cache
        except Exception:
            return []

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        words = text.split()
        
        if text.rstrip().endswith("/model") and text.endswith(" "):
            models = self._get_models()
            for model in models:
                yield Completion(model, start_position=0)
            return
        
        if len(words) >= 2 and words[-2] == "/model" and not words[-1].startswith("/"):
            models = self._get_models()
            current_word = words[-1]
            for model in models:
                if model.startswith(current_word):
                    yield Completion(model, start_position=-len(current_word))
            return

        if text.endswith(" "):
            return

        if words and words[-1].startswith("/"):
            current_word = words[-1]
            for cmd in SLASH_COMMANDS.keys():
                if cmd.startswith(current_word):
                    yield Completion(cmd, start_position=-len(current_word))


# ---------------------------------------------------------------------------
# Fetch available models
# ---------------------------------------------------------------------------

def get_available_models(client: OpenAI) -> list:
    """Fetch available model names from the API."""
    try:
        models = client.models.list()
        return [m.id for m in models.data]
    except Exception as e:
        console.print(f"[warning]Failed to fetch models: {e}[/warning]")
        return []


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = f"""\
You are **agenc**, a concise, direct coding assistant running inside a developer's terminal.

## Role
- Review code, explain logic, find bugs, suggest improvements, and answer questions about the repo.
- Be direct and technical. Lead with the answer or action, then add detail only if needed. Skip pleasantries.

## Working directory
{CWD}

## Principles
1. **Gather context before acting.** Use `list_files` or `search_files` to find files, `read_file` to read them before editing. Don't guess at paths or contents.
2. **Be specific.** Reference file names, line numbers, and function names.
3. **Think before you conclude.** Trace the actual code flow step-by-step. Follow the data, verify assumptions. Distinguish display logic from functional logic. Don't report issues until you've traced the full execution path.
4. **Clarify before acting.** For tasks involving edits — especially across multiple files — ask one focused clarifying question if the scope or intent is unclear. For read-only tasks (review, explain, search), proceed directly. Don't make assumptions about unstated requirements; if a request has multiple reasonable interpretations, state them and ask which is intended.
5. **Plan before editing.** For changes touching more than one file, briefly state what you intend to do and ask for confirmation before making any edits.
6. **Give actionable feedback.** Concrete suggestions — not vague observations.
7. **When reviewing, look for:** bugs, edge cases, naming, structure, performance, security, readability.

## Tools

You have exactly 6 tools. You cannot run arbitrary shell commands.

### `list_files(path, all, recursive)`
List files and directories. `path` defaults to ".". Set `recursive=True` for recursive listing. `all=True` includes hidden files.

### `search_files(path, pattern)`
Find files by glob pattern. `pattern` examples: `"*.py"`, `"test_*"`, `"**/*.md"`.

### `search_text(path, pattern, include)`
Search file contents for text/regex. Returns matching lines with file:line numbers. `include` filters files (e.g. `"*.py"`).

### `read_file(path, offset, limit)`
Read file contents. `offset` is 1-indexed line number (default: 1). `limit` is max lines (default: 2000). Files >1MB are rejected.

### `create_file(path, content)`
Create a new file or overwrite an existing one. User must approve.

### `edit_file(path, old_str, new_str)`
Surgical string replacement. `old_str` must match exactly once. User sees a diff and must approve.

## Behavioral guidelines
- Only make the changes you were asked to make. Do not modify behaviour or refactor beyond scope.
- **Do not commit changes** — you don't have git tools.
- Prefer simple, direct solutions. Avoid unnecessary abstractions.
- Never echo or log secrets, tokens, API keys, or credentials.
- Use markdown for formatting. Keep responses concise.

### Math formatting
Do **not** use LaTeX math syntax (e.g. `$...$`, `$$...$$`, `\\frac`, `\\sum`, etc.). The terminal cannot render LaTeX. Instead:
- Use plain text: `x² + y² = z²` or `x^2 + y^2 = z^2`
- Use Unicode: `π`, `√`, `±`, `∞`, `∫`, `∑`, `Π`
- For equations, use code blocks with ASCII/Unicode:
  ```
  x = (-b ± √(b² - 4ac)) / 2a
  ```
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


def make_session(client=None) -> PromptSession:
    """Create a PromptSession with multi-line support."""
    bindings = KeyBindings()

    @bindings.add("enter")
    def _(event):
        event.current_buffer.validate_and_handle()

    @bindings.add("escape", "enter")
    def _(event):
        event.current_buffer.insert_text("\n")

    return PromptSession(
        key_bindings=bindings,
        multiline=True,
        completer=SlashCommandCompleter(client),
    )


def make_client() -> OpenAI:
    if not API_KEY:
        console.print(
            "[warning]Warning: No API key found. Set AGENC_API_KEY or OPENAI_API_KEY.[/warning]"
        )
    return OpenAI(base_url=DEFAULT_BASE_URL, api_key=API_KEY or "unused")


def chat_turn(client: OpenAI, messages: list, model: str) -> tuple[str, TokenUsage]:
    """Run one turn of the agentic loop."""
    turn_usage = TokenUsage()
    
    while True:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
        )

        if response.usage:
            turn_usage.add(
                response.usage.prompt_tokens,
                response.usage.completion_tokens,
                response.usage.total_tokens
            )

        msg = response.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))

        if not msg.tool_calls:
            return msg.content or "", turn_usage

        for tc in msg.tool_calls:
            fn = tc.function
            try:
                args = json.loads(fn.arguments)
            except json.JSONDecodeError:
                args = {"command": fn.arguments}

            display_tool_call(fn.name, args)

            if fn.name == "list_files":
                result = handle_list_files(args)
            elif fn.name == "search_files":
                result = handle_search_files(args)
            elif fn.name == "search_text":
                result = handle_search_text(args)
            elif fn.name == "read_file":
                result = handle_read_file(args)
            elif fn.name == "create_file":
                result = handle_create_file(args)
                console.print(f"  [info]{result}[/info]")
            elif fn.name == "edit_file":
                result = handle_edit_file(args)
                console.print(f"  [info]{result}[/info]")
            else:
                result = f"Unknown tool: {fn.name}"


            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
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
- `/model <name>` — switch model (tab-complete model names)
- `/models` — list available models
- `/tokens` — show token usage for this session
- `/onboard` — explore the repository and summarize its structure and purpose
- `/shell` — run a shell command and optionally add output to context

### Key bindings
- **Enter** — send message
- **Alt+Enter** — insert newline
"""
        )
    )


def print_tokens(usage: TokenUsage):
    """Display session token usage."""
    console.print(
        Markdown(
            f"""\
### Token Usage (session)
| Type | Count |
|------|-------|
| Prompt | {usage.prompt_tokens:,} |
| Completion | {usage.completion_tokens:,} |
| **Total** | **{usage.total_tokens:,}** |
"""
        )
    )


def run_shell_command(command: str) -> tuple[int, str, str]:
    """Run a shell command and return (exit_code, stdout, stderr)."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=SHELL_TIMEOUT,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"Command timed out ({SHELL_TIMEOUT}s limit)"
    except Exception as e:
        return -1, "", f"Error executing command: {e}"


def truncate_output(text: str) -> str:
    """Truncate output to prevent context flooding."""
    if not text:
        return ""
    
    lines = text.splitlines()
    truncated = False
    
    if len(lines) > SHELL_MAX_LINES:
        lines = lines[:SHELL_MAX_LINES]
        truncated = True
    
    result = "\n".join(lines)

    if len(result) > SHELL_MAX_CHARS:
        result = result[:SHELL_MAX_CHARS]
        truncated = True

    if truncated:
        result += f"\n... [truncated at {SHELL_MAX_LINES} lines / {SHELL_MAX_CHARS} chars]"

    return result


def handle_shell_command(messages: list) -> bool:
    """Handle /shell command. Returns True if output was added to context."""
    command = console.input("[bold]Shell command:[/bold] ").strip()
    if not command:
        console.print("[warning]No command provided.[/warning]")
        return False
    
    console.print(f"[tool]Running: {command}[/tool]")
    exit_code, stdout, stderr = run_shell_command(command)
    
    # Display output (unlimited to user)
    if stdout:
        console.print(Panel(stdout, title="stdout", border_style="green"))
    if stderr:
        console.print(Panel(stderr, title="stderr", border_style="yellow"))
    if not stdout and not stderr:
        console.print("[info](no output)[/info]")
    
    console.print(f"[info]Exit code: {exit_code}[/info]")
    
    # Ask if user wants to add output to context
    add_to_context = console.input("[bold]Add output to context? [Y/n]:[/bold] ").strip().lower()
    if add_to_context in ("", "y", "yes"):
        # Truncate for context to prevent flooding
        stdout_truncated = truncate_output(stdout)
        stderr_truncated = truncate_output(stderr)
        
        context_msg = f"""[SHELL COMMAND OUTPUT]
Command: {command}
Exit code: {exit_code}
---
stdout:
{stdout_truncated if stdout_truncated else "(empty)"}
---
stderr:
{stderr_truncated if stderr_truncated else "(empty)"}
---
[END SHELL OUTPUT]"""
        
        messages.append({
            "role": "user",
            "content": context_msg
        })
        console.print("[info]Output added to context.[/info]")
        return True
    else:
        console.print("[info]Output not added to context.[/info]")
        return False


def main():
    global DEFAULT_MODEL

    print_welcome()
    client = make_client()

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    usage = TokenUsage()
    session = make_session(client)
    prompt = HTML("<b><blue>you</blue></b> > ")

    while True:
        try:
            user_input = session.prompt(prompt, style=PROMPT_STYLE).strip()
        except KeyboardInterrupt:
            console.print("\n[info]Input cancelled.[/info]")
            continue
        except EOFError:
            console.print("\n[info]Goodbye![/info]")
            break

        if not user_input:
            continue

        if user_input.startswith("/"):
            cmd_parts = user_input.split(maxsplit=1)
            cmd = cmd_parts[0].lower()
            aliases = get_slash_command_aliases()
            primary_cmd = aliases.get(cmd)

            if primary_cmd == "/quit":
                console.print("[info]Goodbye![/info]")
                break
            elif primary_cmd == "/help":
                print_help()
                continue
            elif primary_cmd == "/clear":
                messages = [{"role": "system", "content": SYSTEM_PROMPT}]
                usage = TokenUsage()
                console.print("[info]Conversation cleared.[/info]")
                continue
            elif primary_cmd == "/model":
                if len(cmd_parts) > 1:
                    DEFAULT_MODEL = cmd_parts[1]
                    console.print(f"[info]Model set to {DEFAULT_MODEL}[/info]")
                else:
                    console.print(Markdown(f"**Current model:** `{DEFAULT_MODEL}`\n\nUsage: `/model <name>`"))
                continue
            elif primary_cmd == "/models":
                models = get_available_models(client)
                if models:
                    current = DEFAULT_MODEL
                    model_list = "\n".join(
                        f"  * {m}" if m == current else f"  {m}"
                        for m in sorted(models)
                    )
                    console.print(f"[info]Available models ({len(models)}):\n{model_list}[/info]")
                else:
                    console.print("[warning]No models available[/warning]")
                continue
            elif primary_cmd == "/tokens":
                print_tokens(usage)
                continue
            elif primary_cmd == "/onboard":
                user_input = """Onboard yourself to this repository concisely. Read the following files if they exist, in order: README.md (or readme.md), then pyproject.toml or package.json or similar dependency/config file. Do not read source files unless the README is missing or contains no useful information. Summarize the project's purpose and structure in a few sentences."""
            elif primary_cmd == "/shell":
                handle_shell_command(messages)
                continue
            else:
                console.print(f"[warning]Unknown command: {cmd}[/warning]")
                continue

        messages.append({"role": "user", "content": user_input})

        try:
            console.print()
            reply, turn_usage = chat_turn(client, messages, DEFAULT_MODEL)
            usage.add(turn_usage.prompt_tokens, turn_usage.completion_tokens, turn_usage.total_tokens)
            console.print()
            console.print(Markdown(reply))
            console.print()
        except KeyboardInterrupt:
            console.print("\n[warning]Interrupted.[/warning]")
            if messages[-1]["role"] in ("user", "assistant"):
                messages.pop()
        except Exception as exc:
            console.print(f"\n[bold red]Error:[/bold red] {exc}")
            if messages[-1]["role"] in ("user", "assistant"):
                messages.pop()


if __name__ == "__main__":
    main()
