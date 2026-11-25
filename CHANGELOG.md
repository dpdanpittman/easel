# Changelog

## Unreleased

- Waiting on you: an open PR someone else wrote that you approved and that is still unmerged after `github.approved_after_h` hours (48 by default, 0 = off) gets an automatic line with its age, merge state and CI. It comes from the same GitHub query as your open PRs.
- A mute list for that line (`github.approved_mute_file`, default `~/.config/easel/approved-mute.txt`, one `repo#n` per line): a muted PR leaves Waiting on you and shows under Open PRs as "watching" (violet, eye mark).
- In flight groups that do not fit rotate like a conveyor: the top row slides up and rejoins at the bottom, and the rotation survives data refreshes. The work task list moves as before.

## 0.1.0 (2026-11-23)

First public release.

- The wall board at 1920x1080: status strip, work tasks (Linear first, notes overrides, state lines from facts), In flight (waiting on you, running now, just finished, open PRs), the grid scene with sunrise, sunset and moon, and Done this week.
- Reads Linear and GitHub read-only; Linear keys come from a `key_cmd` at run time and are never stored.
- Reads the In flight and work list blocks of an append-only notes file, such as an [Essence](https://github.com/dpdanpittman/essence) sketchbook's `current.md`.
- Background agents from a Claude Code hook (`scripts/agents-hook.py`).
- Optional phone remote on a private network, and monitor power by the clock.
- Example systemd units (`easel-server`, `easel-browser`, `easel-power`), a demo on invented data, and an offline test suite.
