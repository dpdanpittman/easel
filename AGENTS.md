# AGENTS.md

Instructions for AI coding agents (and people) working on this repository.

## What this is

A wall board for work hours: a small Python server (standard library only) and a static page. The server reads Linear and GitHub (read-only), a notes file and a Claude Code hook's record of background agents, and serves JSON; the page draws it at 1920x1080. README.md explains every region and rule; read it before changing behaviour.

## Layout

- `server.py` the HTTP server, routes and caches; wires the sources together.
- `config.py` the settings (TOML), defaults and checks. Every module reads settings through `config.get()`.
- `tasks.py` the work task list: the notes file's WORK TASKS block, the Linear fetch (`Linear`), the merge, grouped rows, the waiting/running flags.
- `derive.py` a ticket's state line from Linear, its PRs (`TicketPRs`) and finished lines.
- `inflight.py` the In flight panel: the notes file's IN FLIGHT block, open PRs (`GitHub`), the automatic waiting line, the merge with the agents record.
- `done.py` the Done this week box.
- `checks.py` the newest-run CI rule.
- `agents.py` the hook's writer and the server's reader for background agents; `scripts/agents-hook.py` is the hook.
- `status.py` the status strip tiles and the grid scene's nodes; `sky.py` sun and moon maths.
- `remote.py` the remote's access rules and actions; `power.py` monitor power via wlr-randr.
- `diskcache.py` the last good answers on disk.
- `web/` the page (`index.html`, `screen.js`, `screen.css`), the pure fit rules (`fit.js`), the scene (`grid.js`, `grid.css`), the remote page.
- `scripts/` the demo server on invented data, the screenshot script, the kiosk browser launcher (`easel-browser.sh`), the hook.
- `systemd/` example units.
- `tests/` the test suite; `tests/easel.test.toml` is the settings it runs with.

## Commands

```
python3 -m unittest discover -s tests          # the whole suite, offline (no network, no keys)
node --check web/*.js                          # syntax of the page scripts
python3 -m py_compile *.py scripts/*.py
python3 scripts/demo.py --port 8099            # the board on invented data, http://127.0.0.1:8099/
python3 scripts/screenshot.py                  # docs/screenshot.png from the demo (needs playwright)
```

`tests/test_fit.py` runs `web/fit.js` with node and skips when node is missing.

## Rules

- Read-only towards Linear and GitHub, always. No mutation queries, no `gh` command that changes anything.
- Never store or log a secret. Keys come from `key_cmd` at run time and stay in memory; GitHub goes through `gh`'s own login. A log line carries the source and the error type, never content.
- Everything from Linear, GitHub, the notes file and the agents record is untrusted text: clean it (`tasks._clean`, `inflight.clean`), cut it, and only ever put it in `textContent` on the page.
- The page loads nothing from off the box (the CSP enforces it). No CDNs, no web fonts from elsewhere.
- Every source fails closed and says so in amber; a missing source never makes a list look short or complete.
- Nothing is hidden silently: real counts in every heading, whole rows only, motion only when a whole row cannot be shown.
- Tests use invented names and ticket ids (ACME-, OPS-, WEB-; Sam, Priya). Never put real names, hosts, addresses or tickets in code, tests or docs.
- Keep the standard library only for the server. New settings go in `config.DEFAULTS`, `easel.example.toml` and README in the same change.
- Run the tests and `node --check` before calling a change done, and say plainly what was not run.
