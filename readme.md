# agenc

A coding agent that runs in your terminal and connects to any OpenAI-compatible endpoint. It explores your repo with safe, specific tools — no shell execution.

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

- **Interactive REPL** — conversational coding assistant with history and multi-line input (Alt+Enter for newline, Enter sends)
- **6 specific tools** — no shell execution. Each tool is a safe, bounded operation:
  - `list_files` — directory listing (optional recursive)
  - `search_files` — find files by glob pattern
  - `search_text` — grep-like text search
  - `read_file` — read file contents with offset/limit
  - `create_file` — write new files
  - `edit_file` — surgical string replacement
- **Path jailing** — all file access restricted to the current working directory
- **User confirmation** — file writes require approval (or set `AGENC_AUTO_WRITE=1`)
- **Any OpenAI-compatible endpoint** — works with OpenAI, Ollama, OpenRouter, llama.cpp, vLLM, etc.
- **Rich terminal output** — markdown rendering, syntax highlighting, diff previews
- **Token tracking** — cumulative session usage via `/tokens`

## REPL commands

| Command          | Description                    |
|------------------|--------------------------------|
| `/help`          | Show available commands        |
| `/quit`          | Exit the agent                 |
| `/clear`         | Clear conversation history     |
| `/model <n>`     | Switch model (tab-complete)    |
| `/models`        | List available models          |
| `/tokens`        | Show session token usage       |

### Key bindings

| Key            | Action                         |
|----------------|--------------------------------|
| Enter          | Send message                   |
| Alt+Enter      | Insert newline                 |

## Tools

### `list_files(path, all, recursive)`
List files and directories. `path` defaults to ".". Set `recursive=True` for recursive listing. `all=True` includes hidden files.

### `search_files(path, pattern)`
Find files by glob pattern. `pattern` examples: `"*.py"`, `"test_*"`, `"**/*.md"`.

### `search_text(path, pattern, include)`
Search file contents for text/regex. Returns matching lines with `file:line: content`. `include` filters files (e.g. `"*.py"`).

### `read_file(path, offset, limit)`
Read file contents. `offset` is 1-indexed line number (default: 1). `limit` is max lines (default: 2000). Files >1MB are rejected.

### `create_file(path, content)`
Create a new file or overwrite an existing one. Shows a preview and requires user confirmation.

### `edit_file(path, old_str, new_str)`
Surgical string replacement. `old_str` must match exactly once in the file. Shows a word-level diff and requires user confirmation.

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
config.py       — environment variables, constants
tools.py        — tool schemas, path validation, all handlers
```

**Modules removed:** `sandbox.py`, `git.py`, `gh.py` — replaced with specific tool handlers.

The agent uses a simple agentic loop: it sends the conversation to the model, and if the model responds with tool calls, it executes them and feeds results back until the model produces a final text response.

## Security model

- **No shell execution** — the agent cannot run arbitrary commands. Every operation is a specific, bounded tool.
- **Path jailing** — all file/directory paths are resolved and checked to stay within the working directory.
- **User confirmation** — `create_file` and `edit_file` show previews and require `y/n` approval.
- **File size limits** — files >1MB cannot be read; text search skips large files.
