# agenc

A coding agent that runs in your terminal and connects to any OpenAI-compatible endpoint. It explores your repo with sandboxed shell commands, reviews your code, creates and edits files, and can stage and commit changes.

## Quick start

```bash
# Install (creates a venv + installs deps from pyproject.toml)
make

# Configure your endpoint
export AGENC_API_KEY="sk-..."
export AGENC_BASE_URL="https://api.openai.com/v1"   # default
export AGENC_MODEL="gpt-4o"                          # default

# Or use a local model
export AGENC_BASE_URL="http://localhost:11434/v1"    # Ollama
export AGENC_MODEL="qwen2.5-coder:32b"
export AGENC_API_KEY="unused"

# Or use OpenRouter
export AGENC_BASE_URL="https://openrouter.ai/api/v1"
export AGENC_API_KEY="sk-or-..."
export AGENC_MODEL="anthropic/claude-sonnet-4"

# Run from your repo root
cd /path/to/your/repo
python /path/to/agenc/agent.py

# Or use the Makefile
make run
```

## Features

- **Interactive REPL** — conversational coding assistant in your terminal, with history and multi-line input (paste freely; Alt+Enter inserts a newline, Enter sends)
- **File creation & editing** — `create_file` for new files, `edit_file` for surgical string-replacement edits, both with user confirmation
- **Sandboxed shell** — `ls`, `cat`, `grep`, `find`, `rg`, etc. for exploring code; destructive commands are blocked
- **Git integration** — read repo state (`status`, `diff`, `log`, `blame`, ...) and make commits (`add`, `commit`)
- **GitHub CLI** — read issues and PRs with `gh` (read-only allow-list)
- **Smart output compression** — successful commands are summarized to save context; failures preserve full detail
- **Path jailing** — all file access is restricted to the current working directory
- **Any OpenAI-compatible endpoint** — works with OpenAI, Ollama, OpenRouter, llama.cpp, vLLM, etc.
- **Rich terminal output** — markdown rendering, syntax highlighting, diff previews

## REPL commands

| Command          | Description                    |
|------------------|--------------------------------|
| `/help`          | Show available commands        |
| `/quit`          | Exit the agent                 |
| `/clear`         | Clear conversation history     |
| `/model <n>`     | Switch model mid-session       |
| `/run <cmd>`     | Run a shell command directly   |
| `/tokens`        | Show session token usage       |

### Key bindings

| Key            | Action                         |
|----------------|--------------------------------|
| Enter          | Send message                   |
| Alt+Enter      | Insert newline                 |
| Ctrl+C         | Cancel current input           |

## File editing

The agent has two tools for modifying files:

**`create_file`** — write a new file or overwrite an existing one. The agent provides the full file content. Use for new files or full rewrites.

**`edit_file`** — surgical string replacement. The agent specifies an exact string to find (`old_str`) and its replacement (`new_str`). The match must be unique in the file. Use for focused, minimal edits.

Both tools show a preview and require user confirmation before writing:

```
  ▸ create: src/utils.py (24 lines)
  1 def parse_config(path: str):
  2     ...
  Apply create? [y/n]:

  ▸ edit: src/main.py (line 12)
  - old_value = get_data()
  + old_value = get_data(timeout=30)
  Apply edit? [y/n]:
```

Set `AGENC_AUTO_WRITE=1` to skip confirmations (auto-approve all writes).

## Git support

The agent can interact with git at the subcommand level:

**Allowed (read):** `status`, `diff`, `log`, `show`, `blame`, `shortlog`, `describe`, `branch`, `tag`, `stash list`, `stash show`, `ls-files`, `ls-tree`, `rev-parse`, `rev-list`, `cat-file`, `name-rev`, `reflog`

**Allowed (write):** `add`, `commit`

**Blocked:** `push`, `pull`, `fetch`, `reset`, `rebase`, `cherry-pick`, `merge`, `checkout`, `switch`, `clean`, `rm`, `restore`, and any other subcommand not in the allow-list. The flags `--force`, `--hard`, `--delete`, `-d`/`-D`, `--mirror`, `--bare`, and `--no-verify` are also blocked globally.

## GitHub CLI support

The agent can read from GitHub using the `gh` CLI (must be installed separately):

**Allowed:** `gh issue list/view/status`, `gh pr list/view/status/checks/diff`, `gh repo list/view`, `gh help`, `gh version`

**Blocked:** `create`, `edit`, `close`, `reopen`, `delete`, `merge`, `checkout`, `convert`, `sync`, `ready`, `develop`, and any command with `--body`, `--title`, or `-d`/`--delete` flags.

## Output compression

Command output is automatically compressed to keep the model's context window clean. Content commands (`cat`, `grep`, `git diff`, `git log`, etc.) are never compressed on success. Non-content commands (`ls`, `find`, `tree`, `git status`, etc.) are compressed:

| Scenario                  | Behavior                                           |
|---------------------------|----------------------------------------------------|
| Success, ≤60 lines        | Passed through unchanged                           |
| Success, 61–200 lines     | First 10 + last 20 lines, with omission note       |
| Success, >200 lines       | Last 30 lines + total count                        |
| Failure, ≤120 lines       | Passed through unchanged (full context for debug)  |
| Failure, >120 lines       | Last 80 lines + total count                        |

Stderr is always preserved in full on failure. A byte-level safety net truncates at 100KB regardless.

## Security model

The agent runs in a multi-layer sandbox:

**Layer 1 — Command validation (bash tool):**
- Read-only utilities are allow-listed: `ls`, `cat`, `grep`, `head`, `tail`, `find`, `tree`, `wc`, `rg`, `fd`, `diff`, `awk`, `sed`, `sort`, `uniq`, `cut`, `tr`, etc.
- `git` is validated at the subcommand level (see above).
- `sed -i` (in-place edit) is explicitly blocked.
- Dangerous patterns are rejected with word-boundary matching: `rm`, `mv`, `cp`, `curl`, `python`, `sudo`, `bash`, `$(...)`, backticks, output redirection (`>`, `>>`), etc.

**Layer 2 — Path jailing (all tools):**
- All path arguments are resolved and must stay within `$CWD`.
- Symlink escapes are caught by `Path.resolve()`.

**Layer 3 — User confirmation (write tools):**
- `create_file` and `edit_file` show a preview and require `y/n` approval.
- Set `AGENC_AUTO_WRITE=1` to auto-approve.

**Layer 4 — Resource limits:**
- Shell commands time out after 30 seconds.
- Output is capped at 100KB.

## Configuration

| Env var             | Default                        | Description                          |
|---------------------|--------------------------------|--------------------------------------|
| `AGENC_API_KEY`     | `$OPENAI_API_KEY`              | API key for the model endpoint       |
| `AGENC_BASE_URL`    | `https://api.openai.com/v1`    | OpenAI-compatible endpoint URL       |
| `AGENC_MODEL`       | `gpt-4o`                       | Model name                           |
| `AGENC_AUTO_WRITE`  | off                            | Set to `1` to auto-approve file writes |

## Architecture

```
agent.py        — main entry point, system prompt, REPL loop, agent logic
config.py       — environment variables, allow/block lists, constants
git.py          — git subcommand validation
sandbox.py      — command validation, path jailing, output compression, run_shell()
tools.py        — tool schema, handlers for bash/create_file/edit_file, display helpers
```

**Module dependencies:**
```
agent.py
  └── tools.py
        └── sandbox.py
              └── git.py
              └── config.py
```

The agent uses a simple agentic loop: it sends the conversation to the model, and if the model responds with tool calls, it executes them and feeds results back until the model produces a final text response.
