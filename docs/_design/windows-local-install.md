# Automatic memory on Windows

Status: **accepted** (October 2026; decisions in §7). W1–W4 implemented (transport and lifecycle; psutil ancestry; shell-neutral hook commands checked by doctor; a capture → recall round trip through every shell in the install smoke test). The per-agent check on a Windows machine is still to come (checklist in §8).

On Windows, `astrocyte setup` registers the MCP server, but automatic memory does nothing. The hooks are installed, yet the agent daemon they talk to needs Unix domain sockets (`agentd.supported()` is false on Windows), so nothing is captured and nothing is recalled. `astrocyte doctor` says so. This design makes the hook path work on Windows. Every part can be tested on GitHub's `windows-latest` runners, except one: which shell each agent uses to run a hook. That is called out as needing a Windows user (§4).

---

## 1. What is Unix-only today

| Where | What | Windows |
|---|---|---|
| `agentd.request` / `serve` | `AF_UNIX` socket, chmod 0600 | CPython has no `AF_UNIX` on Windows |
| `agentd.run` | `fcntl.flock` single-instance lock | No `fcntl` |
| `agentd.spawn` | `start_new_session=True` to detach | Ignored; the daemon dies with the hook's console or job |
| `hooks._agent_ancestor_args` | `ps -o ppid= -o args=` for headless detection | No `ps`; every session would count as interactive |
| `server.hook_prefix` | `shlex.quote` (POSIX quoting) | Wrong for cmd and PowerShell; backslash paths are mangled by bash |
| `paths.*` | `~/.config`, `~/.local/share`, `~/.local/state` | Works, but Windows convention is `%APPDATA%` / `%LOCALAPPDATA%` |
| `0o600` / `0o700` modes | Privacy of the store, spool, state | No-ops; privacy comes from profile ACLs (`privacy.py` already skips Windows) |
| Hook stdio | JSON on stdin/stdout | Console code page (cp1252) mangles non-ASCII |
| `os.replace` | Atomic state writes | Fails with `PermissionError` if another process holds the target open |

Everything else (SQLite, fastembed/ONNX, the MCP server, transcripts) is already portable. The MCP server is in fact registered and working on Windows today.

## 2. Transport: loopback TCP with a token

The daemon gets a small transport interface, `listen()` / `connect()`, with two implementations:

- **POSIX: unchanged.** `AF_UNIX` in the state directory, 0600. Filesystem permissions are the authentication.
- **Windows: TCP on `127.0.0.1`, port 0 (the OS picks), plus a 32-byte random token.**
  - At start-up the daemon writes `{"port", "token", "pid"}` atomically to `agentd.json` in the state directory. That directory is under `%LOCALAPPDATA%`, which only the user (plus SYSTEM and Administrators) can read.
  - Every request carries the token, compared with `hmac.compare_digest`. A wrong or missing token is dropped without a reply.
  - Binding to loopback doesn't trigger a Windows Firewall prompt.

**Why not named pipes:** asyncio serves them only through the Proactor loop's undocumented `start_serving_pipe`, and a client needs Win32 file APIs (pywin32, or ctypes). TCP with a token is standard library on both ends. It can also be **tested on Linux and macOS CI** by forcing the Windows transport there (`ASTROCYTE_AGENTD_TRANSPORT=tcp`), so most of the Windows code path runs on every PR, not only on the Windows job.

The newline-delimited JSON protocol is unchanged.

## 3. Process lifecycle

- **Single instance:** `msvcrt.locking(fd, LK_NBLCK, 1)` on `agentd.lock`, behind the same `_acquire_lock()` used for `fcntl.flock` on POSIX. Losing the race means another daemon owns the port, so exit 0, as today.
- **Detached spawn:** `creationflags = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW | CREATE_BREAKAWAY_FROM_JOB`, `close_fds=True`.
  - **Risk:** agent CLIs (Node-based) may run hooks inside a job object with kill-on-close. Breakaway is then what keeps the daemon alive after the session ends.
  - If the job forbids breakaway, `CreateProcess` fails. We retry without the flag, and the daemon lives only as long as the session. That is acceptable, because the next session's hook restarts it. Doctor reports which case applies.
- **Headless detection:** `psutil` on Windows (`psutil; sys_platform == "win32"`, about 1 MB) to walk parents and read their command lines. The `-p` / `exec` flags are in the command line, and `CreateToolhelp32Snapshot` alone gives only names. POSIX keeps `ps`, so no new dependency there.
- **UTF-8 stdio:** hooks call `sys.stdin.reconfigure(encoding="utf-8")` and the same for stdout, before reading the payload.
- **Replace with retry:** `_atomic_write` retries `os.replace` on `PermissionError` (3 × 50 ms) before giving up. Hook state files are tiny and rarely contended.

## 4. Hook commands: the one thing CI cannot settle

Each agent runs a hook's command string through some shell, and the quoting must match it. What we know:

| Agent | Shell for hooks on Windows | Source |
|---|---|---|
| Copilot CLI | Its own choice per entry: hook entries have separate `bash` and `powershell` fields | GitHub's hooks reference |
| Claude Code | Not stated for hooks. Claude Code uses Git Bash when it finds one (`CLAUDE_CODE_GIT_BASH_PATH` overrides) and PowerShell otherwise; "Git for Windows is optional". So either, depending on the machine | code.claude.com troubleshoot-install |
| Codex CLI | Not documented | — |
| Antigravity | Not documented | — |

**Design: a command that is valid in all three shells.** Instead of guessing a shell, write a command that cmd, PowerShell and Git Bash all parse the same way:

- the same interpreter-and-module form as on POSIX (`<tool python> -I -m astrocyte.cli hook …`). `-I` keeps an inherited `PYTHONPATH` out (pinned by `test_registered_commands_ignore_an_inherited_pythonpath`); uv's `astrocyte.exe` shim does not isolate, so it is not used;
- **forward slashes** (`C:/Users/alice/AppData/Roaming/uv/tools/astrocyte/Scripts/python.exe`), which bash does not mangle and Windows `CreateProcess` accepts;
- **no spaces:** if the path has any (a user name with a space), use its 8.3 short form (`GetShortPathNameW`). If 8.3 names are disabled on the volume, setup fails loudly with the manual fix rather than writing a hook that silently does nothing;
- no shell syntax at all: no quotes, `||`, or redirections. Today's commands already rely on the hook process exiting 0, not on `|| true`.

```
C:/Users/alice/AppData/Roaming/uv/tools/astrocyte/Scripts/python.exe -I -m astrocyte.cli hook prompt --host claude
```

For Copilot, setup writes this same string into both `bash` and `powershell`.

**`astrocyte doctor` runs each registered hook command for real** through cmd, PowerShell and bash (whichever exist), with a `{"ping": true}` payload that the hook answers without side effects. On any OS, a quoting or path bug then shows up in doctor rather than as silent loss of memory.

**What CI proves, and what it doesn't.** The `windows-latest` job runs the real installed shim through `cmd /c`, `pwsh -c` and Git Bash with real payloads, and checks the capture → daemon → recall round trip. It cannot run Claude Code, Codex or Antigravity sessions (they need accounts), so it cannot see which shell those agents actually pick, or any Windows-specific payload field. That needs a **one-time check by a Windows user per agent**: a checklist in the docs, and the result recorded in the table above. The shell-agnostic command keeps the stakes of that unknown low.

## 5. Paths

**Unchanged, decided in W1.** Config, data and state stay under the user's profile (`~\.config\astrocyte\`, `~\.local\share\astrocyte\`, `~\.local\state\astrocyte\`), where setup has always put them on Windows. The `%APPDATA%` / `%LOCALAPPDATA%` convention would have meant precedence rules for existing files and every test that builds a sandbox profile learning a second layout, for no change in behaviour. `XDG_*` variables still win. Revisit if Windows users ask.

## 6. Plan (PRs, in order)

Since #105 (v0.18.0), CI runs `astrocyte-py`, `astrocyte-sqlite` and the install smoke test on `windows-latest` for every change. Tests that need what this design adds are marked in `tests/platform_compat.py` and skip on Windows; each PR below removes the marks it makes obsolete. W1 removed `needs_unix_socket`: the daemon tests run on Windows, and on every OS over both transports.

| PR | Content | Proven by |
|---|---|---|
| **W1** | Transport interface; TCP + token transport; `msvcrt` lock; Windows spawn flags; Windows paths; `supported()` true on Windows | Unit tests on Linux/macOS with the TCP transport forced; on the `windows-latest` CI job (#105), the tests now marked `needs_unix_socket` run instead of skipping |
| **W2** | `psutil` ancestry on Windows; UTF-8 stdio; `os.replace` retry | `windows-latest`: a headless parent (`python -c` posing as `claude -p`) is detected |
| **W3** | Shell-agnostic hook commands; Copilot `powershell` field; hook `ping`; doctor runs registered commands through each shell | `windows-latest`: setup in a scratch profile, then each registered command through cmd/pwsh/bash, then the capture → recall round trip |
| **W4** | Smoke test extended to automatic memory (hooks + daemon) on Windows; quick-start docs; the per-agent verification checklist | The smoke test already runs on Windows for PRs and release tags (#105); W4 adds the capture → recall round trip to it |

The full test suite does not need to run on Windows: only the harness, the SQLite adapter and the smoke test. A Windows job costs about twice a Linux one.

## 7. Decisions (2026-10-04)

1. **`psutil` as a Windows-only dependency** for process ancestry.
2. **The one-time per-agent check on Windows is Calvin's** (assigned 2026-10-05), using the checklist in §8. Until it is done, each agent's row in §4 stays "not verified", and the docs say so.
3. **WSL is the recommended Windows path** in the docs until W1–W4 land and the per-agent check is done: an agent running inside WSL is Linux, and automatic memory works there today.

## 8. Per-agent check on a Windows machine (checklist)

About 15 minutes, on a Windows machine where at least one agent is installed and signed in. Use PowerShell, and a Windows user name with a space in it if one is available (it exercises the 8.3 short-path rule). Record each result in §4's table and in the PR that closes this check.

**Install**
1. Install uv: `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`, then open a new terminal.
2. `uv tool install astrocyte`, then `astrocyte --version` (0.20.0 or later).
3. `astrocyte setup`. Note what it reports for each agent. For Codex, run `/hooks` in Codex and trust the astrocyte hooks.
4. `astrocyte doctor`. Every hooks line should say "automatic memory on", and nothing should be reported as failing under cmd, PowerShell or Git Bash. Paste the output into the PR.

**Per agent** (Claude Code, Codex, Antigravity: in that order of priority)
5. In a git repository, start an interactive session (not `-p` / `exec`) and send: *"Note for later: our staging deploys go out on Tuesdays."* Let the agent answer, then end the session.
6. `astrocyte memory list` in that repository: the turn is there, with the agent's name as its source, within about 20 seconds.
7. Start a new session in the same repository. Its first context should include "Where you left off" with that exchange. Then ask: *"When do staging deploys go out?"* and check that the answer uses the memory.
8. Claude Code only, optional: `astrocyte setup --claude --file-recall`, ask the agent to read a file it edited in step 5's session (if any), and check that the earlier turn is added after the read.

**Copilot CLI** (recall only; it doesn't capture its own turns)
9. After step 5 in another agent, start `copilot` in the same repository and ask the question from step 7. The memory should be added to the prompt.

**Record**
- Per agent: works / doesn't, and anything `astrocyte doctor` or `%USERPROFILE%\.local\state\astrocyte\agentd.log` said.
- Which shell the agent ran the hook through is not visible from here, and doesn't need to be: the command is the same in all three, and `doctor` has already run it through each. If an agent still fails, the shell is not the cause: note the agent's version, its hook output if it shows any (Claude Code: `claude --debug`), and the end of `agentd.log`.
