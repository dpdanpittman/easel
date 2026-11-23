#!/usr/bin/python3 -I
"""Claude Code hook: records background agents for the easel's Running now / Just finished (agents.py).

Add it to ~/.claude/settings.json for the events SubagentStart, SubagentStop and PostToolUse (matcher "Agent"):

    {"hooks": {
      "SubagentStart": [{"hooks": [{"type": "command", "command": "/path/to/easel/scripts/agents-hook.py"}]}],
      "SubagentStop":  [{"hooks": [{"type": "command", "command": "/path/to/easel/scripts/agents-hook.py"}]}],
      "PostToolUse":   [{"matcher": "Agent",
                         "hooks": [{"type": "command", "command": "/path/to/easel/scripts/agents-hook.py"}]}]}}

It reads the event on stdin, writes only the easel's agents state file (settings `agents.state_file`), never reads a
transcript's content, and always exits 0: it can never block or fail a tool call.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    import agents
except Exception as e:                       # a broken install must not break the session
    print("easel-agents-hook: %s" % type(e).__name__, file=sys.stderr)
    sys.exit(0)

sys.exit(agents.hook_main())
