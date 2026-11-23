# Changelog

## 0.1.0 (2026-11-23)

First public release.

- The wall board at 1920x1080: status strip, work tasks (Linear first, notes overrides, state lines from facts), In flight (waiting on you, running now, just finished, open PRs), the grid scene with sunrise, sunset and moon, and Done this week.
- Reads Linear and GitHub read-only; Linear keys come from a `key_cmd` at run time and are never stored.
- Reads the In flight and work list blocks of an append-only notes file, such as an [Essence](https://github.com/dpdanpittman/essence) sketchbook's `current.md`.
- Background agents from a Claude Code hook (`scripts/agents-hook.py`).
- Optional phone remote on a private network, and monitor power by the clock.
- Example systemd units (`easel-server`, `easel-browser`, `easel-power`), a demo on invented data, and an offline test suite.
