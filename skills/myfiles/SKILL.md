---
name: myfiles
description: "Operate myfiles, the tool that version-controls machine configuration files (dotfiles) with git via symlinks: capture, deploy, eject, status, fix, diff, ignore, ls, including remote SSH hosts (remotes/). Use when the user wants to capture, deploy, eject, check, repair or compare configuration files, locally or on a remote host."
argument-hint: "Describe the myfiles operation you want, e.g.: capture ~/.config/app, status --remotes, fix --only drift, diff /etc/fstab, capture host:/home/user/..."
user-invocable: true
---

# myfiles — managing configuration through symlinks

`myfiles` version-controls a machine's configuration files with Git by deploying them as **absolute symlinks** pointing straight into the repository. No copies, no templates: what git versions is exactly what is used.

Key principles:

- **No configuration file** (`myfiles.config.toml` no longer exists), no indirection layer, no git wrapper: the contents of `files/` (+ `remotes/`) **are** what gets versioned, and the repository's `.gitignore` decides the rest.
- **`myfiles` never creates commits** (git is even optional): the user stages and commits `files/` themselves.
- Vocabulary: always **`ignore`**, never `exclude`.
- Documentation sources: `README.md` and `SPEC.md` at the repository root; help for every command via `myfiles <cmd> --help`.

## Golden rules

1. **Never guess the base-dir.** Resolution order: `--base-dir/-d <repo>` → `MYFILES_BASE_DIR` environment variable → discovery of a `files/` directory walking up from the current directory. When in doubt, run the command from inside the dotfiles repository or pass `--base-dir` explicitly.
2. **Preview before modifying.** `capture`, `deploy`, `eject`, `ignore` print their plan then ask for confirmation (`[Y/n]`). `--dry-run` only previews (the output starts with a `dry-run` notice) — use it first, almost always.
3. **`status`, `ls`, `diff` are read-only**: no confirmation, no change.
4. **Safety**: conflicts or destructive operations are refused without `--force`; `eject` guarantees the user is never locked in; every command is idempotent (safe to re-run).

## Repository layout

`files/` (the base-dir) **mirrors the filesystem root**: the leading `/` of a target path is stripped.

| Target                      | Tracked in `files/`                         |
| --------------------------- | ------------------------------------------- |
| `/etc/fstab`                | `files/etc/fstab`                           |
| `~/.config/app/config.yaml` | `files/home/<user>/.config/app/config.yaml` |

Deployed links look like `~/.config/app/config.yaml -> <repo>/files/home/<user>/.config/app/config.yaml`, or a **dir-link** `~/.config/zsh -> <repo>/files/home/<user>/.config/zsh`.

- **Dir-links are first-class**: `capture <dir>` turns a whole directory into a single symlink. A valid dir-link is a healthy state (`status` reports nothing, `deploy` leaves it alone, `fix` never offers it); only a **dangling/foreign** dir-link is a problem that `deploy`/`fix` replace with a real directory + individual links.
- `remotes/<host>/…` mirrors a remote SSH host (same layout, copied — never symlinked).
- The repository's `.gitignore` is **managed by myfiles** (`myfiles ignore` / `--ignore`), with a `# managed by myfiles` header.

## Accepted paths (any command taking paths)

All equivalent: **absolute target** (`~/.config/app`, `/etc/fstab`) · **tracked path** inside `files/` · **root-relative path** (`etc/fstab`) · **remote** `host:/abs/path` or a path under `remotes/`.

## Commands

### `capture <path>... [--ignore <glob>...] [--force] [--remotes [<host>...]] [--dry-run]`

Moves files from the system into `files/`, then (re)creates the symlinks.

- A **file** is moved (`move-and-link`); a **directory** is captured as a **dir-link** (already fully managed → `convert to dir-link`, otherwise `move-and-link … (dir-link)`).
- A directory **already captured** (managed symlink) → `skip (already captured)` (idempotence).
- **Safety**: any **directory** can be captured (no "allowed roots" restriction): only `/` itself and a source **inside the base-dir** are rejected.
- `--ignore <glob>` (repeatable, gitignore syntax, relative to the captured path): excludes entries from the drift check **and appends them to the repository's `.gitignore`** (resolved to their tracked location, e.g. `files/home/<user>/.config/zsh/.antidote`) — for transient files (histories, caches) that a program writes inside a dir-link. Entries present on disk are moved into `files/` so they keep working through the dir-link.
- **Drift** (a real file whose content differs from the tracked copy, a foreign/misplaced symlink…) → **refused** unless `--force` (the system content becomes the new tracked copy). With `--force`, refused if the tracked file has uncommitted git changes (no version is lost).

```bash
myfiles capture /etc/fstab
myfiles capture ~/.config/zsh --ignore .zsh_history --ignore .antidote   # dir-link
myfiles capture ~/.config/app --dry-run
```

### `deploy <path>... [--force] [--remotes [<host>...]] [--dry-run]`

Creates the symlinks for the tracked files. A `PATH` is **required** unless `--remotes` is given (no implicit "deploy everything").

- Target absent → symlink created; already a correct managed symlink → `skip`; regular file with identical content → replaced by the symlink; **conflict** (different content, foreign symlink, directory) → error **without `--force`** (with `--force`: replaced, original kept as `target.bak`).
- An intermediate directory that is a **valid dir-link** → left alone (`skip …: linked via dir-link`), files served through it. A **dangling/foreign** dir-link → replaced by a real directory, files linked individually. A foreign intermediate symlink → refused without `--force`.

```bash
myfiles deploy etc/fstab ~/.config/app
myfiles deploy $(myfiles ls)   # feed a list back in
```

### `eject [<path>...] [--dry-run]`

Replaces every managed symlink with a **real copy** and removes the tracked files from `files/` — as if the tool had never been used. Works even if the link is missing (content restored at the target). A non-managed link is left untouched. No `PATH` → the whole `files/` is ejected.

### `status [<path>...] [--remotes [<host>...]]`

Read-only. Prints **only problems** (lines `label <target>`), nothing when healthy; exit code `1` if any problem. Labels: `dangling` (broken symlink) · `foreign` (non-managed symlink) · `elsewhere` (managed, pointing to the wrong tracked file) · `directory` (target is a directory) · `not-linked` (identical file not yet linked) · `drift` (file whose content differs) · `missing` (target absent) · `not tracked`. Ends with a `run 'myfiles fix' …` hint when problems are found.

### `fix [<path>...] [--defaults] [--only <errtype>...] [--remotes [<host>...]] [--dry-run]`

Resolves the problems reported by `status` **one by one**, interactively (a REPL) — there is deliberately **no `fix --all`**.

| Problem                                                                            | Actions                                                                                              | Default                                                  |
| ---------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- | -------------------------------------------------------- |
| `dangling`/`foreign`/`elsewhere`/`not-linked`/`missing`, dangling/foreign dir-link | `deploy`, `skip`                                                                                     | `deploy`                                                 |
| `drift`                                                                            | `deploy` (tracked wins, system kept as `.bak`) · `capture` (system wins) · `diff` (inspect) · `skip` | **none** (an explicit choice is required; Enter re-asks) |
| `directory`                                                                        | `skip`                                                                                               | `skip` (not auto-fixable, reported only)                 |

Keys: `d`=deploy · `c`=capture · `i`=diff (inspect, then re-asks) · `s`=skip. Each choice is confirmed and applied immediately (`[Y/n]`). `--defaults` runs non-interactively (drifts without a default are skipped); `--only drift` restricts to one problem type; `Ctrl-C` aborts the whole session (exit `130`, already-applied changes are kept).

```bash
myfiles fix            # everything, interactively
myfiles fix --only drift --defaults
```

### `diff <path>`

Like `diff -Naur`, but the two sides are **ordered by modification date**: the older file is `-` (before), the newer one `+` (after) — handy to inspect a drift before deciding (the most recently modified side is always the `+` one).

### `ignore <path>... [--dry-run]`

Adds paths to the repository's `.gitignore` (resolved to their tracked location under `files/`), so git never versions them. Creates `.gitignore` with the `# managed by myfiles` header if absent; already present → `nothing to ignore`.

```bash
myfiles ignore ~/.config/zsh/.antidote ~/.config/app/cache
```

### `ls [--remotes [<host>...]]`

Lists the versioned files (targets with a leading `/`), one per line, honoring `.gitignore` — handy to feed a list back into `deploy`/`status`.

## Remote SSH hosts (`remotes/`)

- Connection details are delegated to SSH (`ssh <host>` must resolve the host).
- **No symlinks on a remote**: files are **copied** host ↔ repo, **only when their SHA-256 differs**.
- `--remotes` is **unified** on `capture`/`deploy`/`status`/`fix`/`ls`: `nargs="*"`, takes **0 to N host names** (no value = every host). An unknown host (no `remotes/<host>`) is an error.
- `capture host:/path` = copy host → `remotes/host` if it differs (`skip (identical)` otherwise); `capture --remotes host` = **recapture** every known file of the host that differs on it.
- `deploy --remotes [hosts]` = copy repo → host if it differs (remote parents created).
- `status --remotes` = remote differences only: `drift` (shows `local:`/`remote:` dates, most recent side marked `(newer)`) and `missing`; hint `run 'myfiles fix --remotes'`; exit `1` if any difference.
- `fix --remotes` = same REPL: `drift` → deploy/capture/diff/skip (**no default**); `missing` → deploy/skip (default deploy).
- `ls --remotes host` = lists `host:/path`.

```bash
myfiles capture host:/home/user/.config/app/config.yaml
myfiles deploy --remotes host
myfiles status --remotes
myfiles fix --remotes --defaults
myfiles diff host:/home/user/.config/app/config.yaml
```

## Pitfalls & anti-patterns

- `-d/--base-dir` is a **per-subcommand** option: `myfiles status -d ~/Dev/config/dotfiles` (not `myfiles -d … status`).
- **Never test on the real system**: to try or reproduce, isolate with `--base-dir <temp-repo>` + `--root-dir <temp-root>`. Otherwise a command applies to the real `files/` and the real links.
- **Removed flags/commands** not to use: `recapture` (replaced by `capture --remotes`), `fix --all`, any config file, `--exclude`.
- `--remotes` takes an optional value, so argparse can swallow a path placed right after it: a token starting with `/` or `host:/` is still recognized as a PATH, but when in doubt give `--remotes` before the paths (or avoid mixing them).
- `drift` **never has a default** in `fix`: do not "confirm" without an explicit choice.
- Do not capture `/` itself nor a source inside the base-dir.
- Never forget: `myfiles` commits nothing — after a `capture`/`ignore`, offer (or make) the `git add`/`commit` of the dotfiles repository.

## Typical workflows

```bash
# overall state before any operation
myfiles status

# repair everything auto-fixable, without asking
myfiles fix --defaults

# see what a capture would do before applying it
myfiles capture ~/.config/app --dry-run

# capture a whole config dir as a dir-link, ignoring transient files
myfiles capture ~/.config/app --ignore '*.bak' --ignore 'cache'

# synchronize a remote host
myfiles status --remotes host && myfiles fix --remotes host
```
