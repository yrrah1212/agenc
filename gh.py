"""
agenc — GitHub CLI (gh) subcommand validation.

This module validates gh commands against an allow-list and blocks
dangerous write operations.
"""

from typing import Optional

from config import ALLOWED_GH_ACTIONS, ALLOWED_GH_TOPIC_COMMANDS, BLOCKED_GH_PATTERNS


def validate_gh_command(parts: list[str]) -> Optional[str]:
    """Validate a gh command.  `parts` is the shlex-split command starting with 'gh'.
    
    Returns an error message if blocked, else None.
    """
    # Check for dangerous patterns first
    full_args = " ".join(parts[1:])
    if BLOCKED_GH_PATTERNS.search(full_args):
        return "Blocked: gh command contains a forbidden action or flag."

    # Find the topic and action (e.g. gh issue list → topic=issue, action=list)
    args = parts[1:]
    topic = None
    action = None
    
    for i, arg in enumerate(args):
        if arg.startswith("-"):
            continue  # skip flags
        if topic is None:
            topic = arg
        elif action is None:
            action = arg
            break
    
    # Topic-only commands (e.g. gh issue, gh pr) are allowed
    if topic is None:
        return "Blocked: gh command with no topic."
    
    if topic in ALLOWED_GH_TOPIC_COMMANDS and action is None:
        return None
    
    # Check if the topic-action combination is allowed
    if action is not None:
        if (topic, action) in ALLOWED_GH_ACTIONS:
            return None
        # Also allow if topic alone is in topic commands but action is not explicitly blocked
        # This is permissive for safe subcommands
        if topic in {"help", "version", "status", "api"}:
            return None
    
    return f"Blocked: 'gh {topic} {action}' is not in the allow-list."
