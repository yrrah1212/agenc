"""
agenc — GitHub CLI (gh) subcommand validation.

This module validates gh commands against an allow-list and blocks
dangerous write operations.
"""

from typing import Optional

from config import ALLOWED_GH_ACTIONS, BLOCKED_GH_PATTERNS


def validate_gh_command(parts: list[str]) -> Optional[str]:
    """Validate a gh command.  `parts` is the shlex-split command starting with 'gh'.
    
    Returns an error message if blocked, else None.
    """
    # Check for dangerous patterns first
    full_args = " ".join(parts[1:])
    if BLOCKED_GH_PATTERNS.search(full_args):
        return "Blocked: gh command contains a forbidden action or flag."

    # Extract topic and action (e.g. gh issue list → topic=issue, action=list)
    args = parts[1:]
    topic = None
    action = None
    
    for arg in args:
        if arg.startswith("-"):
            continue  # skip flags
        if topic is None:
            topic = arg
        elif action is None:
            action = arg
            break
    
    if topic is None:
        return "Blocked: gh command with no topic."
    
    # Build command tuple and check against allow-list
    if action:
        cmd_tuple = (topic, action)
    else:
        cmd_tuple = (topic,)
    
    if cmd_tuple in ALLOWED_GH_ACTIONS:
        return None
    
    # Format error message correctly for topic-only or topic+action
    if action:
        return f"Blocked: 'gh {topic} {action}' is not in the allow-list."
    else:
        return f"Blocked: 'gh {topic}' is not in the allow-list."
