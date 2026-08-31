# myfiles — Specification (SPEC)

## Overview

`myfiles` is a Python CLI that version-controls the configuration files of a machine with Git.

Workflow:

1. **Capture** — configuration files are moved from the system into the *base directory* (a Git-managed directory that mirrors the system's filesystem layout).
2. **Deploy** — symlinks are created at the original system locations, pointing into the base directory.
3. **Eject** — managed symlinks are replaced by real copies, so the system works as if the tool had never been used.

Design goals:

- **Safety first** — no data loss. By default, conflicting or destructive operations are refused unless `--force` is given.
- **Freedom to leave** — `eject` guarantees the user is never locked into the tool.
- **Idempotence** — every command can be re-run safely with the same result.
- **Root fallback** — if a filesystem change is refused for lack of permissions (e.g. a target under `/etc`), the operation is retried with `sudo`.

## Technologies

- Python (>= 3.14)
- `argparse` for the CLI

## Terminology

| Term | Meaning |
| --- | --- |
| base directory (`base-dir`) | The root directory holding the tracked configuration files. |
| tracked file | A file stored in `base-dir`. |
| target location | The absolute system path a tracked file mirrors (e.g. `/etc/fstab`). |
| managed symlink | A symlink created by `myfiles`, pointing from a target location into `base-dir`. |
| dir-link | A managed symlink of a whole directory: `dir -> base-dir/rel`, mirroring the whole directory in the repo. |

## Base directory layout

`base-dir` mirrors the filesystem starting from the root: the leading `/` of the target's absolute path is stripped to obtain the relative path inside `base-dir`.

| Target location | Tracked file in `base-dir` |
| --- | --- |
| `/etc/fstab` | `etc/fstab` |
| `/home/user/.bashrc` | `home/user/.bashrc` |
| `/home/user/.config/app/config.yaml` | `home/user/.config/app/config.yaml` |

```text
The base-dir usually lives in a `files/` subdirectory of the dotfiles repository, whose root holds the project files (`AGENTS.md`, `.gitignore`, ...):

```text
~/Dev/config/dotfiles/
├── AGENTS.md
├── .gitignore                    # managed by `myfiles ignore`
├── .editorconfig
└── files/                        # the base-dir
    ├── etc/
    │   └── fstab
    └── home/user/
        ├── .bashrc
        └── .config/app/config.yaml
```

Rules:

- Capturing the filesystem root `/` itself is rejected.
- A file directly at the root (e.g. `/fstab`) maps to `base-dir/fstab`.
- Symlinks in the *source* path are resolved first: the real location is what gets mirrored.
- `.gitignore` at the repository root is managed by the `ignore` command (see *Ignoring files*).

## Remote hosts (`remotes/`)

myfiles can also version files living on other machines, reached over SSH. Remote hosts are mirrored in a **`remotes/` directory** — a sibling of `files/` at the repository root: each subdirectory of `remotes/` is a **host**, and its contents mirror that host's filesystem exactly like `files/` mirrors the local one (the leading `/` of a remote path is stripped).

```text
~/Dev/config/dotfiles/
├── files/                       # the local base-dir
│   └── ...
└── remotes/
    └── ender3/                  # the `ender3` SSH host
        └── home/admin/
            └── printer_data/config/
                ├── printer.cfg
                └── moonraker.conf
```

Rules:

- All connection details are delegated to the user's SSH configuration: `ssh <host>` must resolve the host (e.g. `Host ender3` in `~/.ssh/config.d/home`).
- There are **no symlinks on a remote host**: files are **copied** in one direction or the other — `capture` copies host → repo, `deploy` copies repo → host — and **only when their content differs**.
- All comparisons are done **by SHA-256 hash** (a file is copied only when the two hashes differ).
- A remote path is written `{host}:/abs/path` (e.g. `ender3:/home/admin/printer_data/config/printer.cfg`). Every command that accepts file or directory names also accepts this syntax, equivalent to the corresponding tracked path under `remotes/<host>`.
- The `--remotes` option is **unified** across `capture`, `deploy`, `status`, `fix` and `ls`: it takes **zero or more host names** (`--remotes [<host>...]`). With no value it applies to every host; with one or more, it restricts to those hosts. An unknown host (no `remotes/<host>` directory) is an error.

## Safety scope for `capture`

For safety, only **directories** are restricted: a directory can be captured only if it is inside one of the following roots:

- `/etc`
- `/usr`
- the XDG config home (default `~/.config`)
- the XDG data home (default `~/.local/share`)

Directories outside these roots are rejected. **Individual files** are always allowed, wherever they are (e.g. `~/.bashrc`). The XDG paths are resolved with `xdg-base-dirs`, so `XDG_CONFIG_HOME`/`XDG_DATA_HOME` are honored when set.

Capturing a source that is inside the base directory is **forbidden** (it would move the repository into itself).

## Ignoring files

There is no configuration file: what `myfiles` version-controls is exactly what lives in `base-dir`, and what git tracks is controlled by `.gitignore`.

- **`capture`** ignores only what you pass with `--ignore`; for a directory capture, the matched entries are appended to the repository's `.gitignore` (see `capture`).
- **`myfiles ignore <path>...`** records paths you never want versioned (e.g. `~/.config/zsh/.antidote`, `~/.config/htop/htop_history`) in the repository's `.gitignore`, resolved to their tracked location (e.g. `files/home/user/.config/zsh/.antidote`). It creates `.gitignore` with a `# managed by myfiles` header if absent, appends new entries, and deduplicates.

```bash
myfiles ignore ~/.config/zsh/.antidote ~/.config/htop/htop_history
```

- This matters when a directory is deployed as a **dir-link** (the whole directory mirrors the repo): files that a program writes there land in `base-dir` and must be ignored by git.

## Symlinks

- Links are created **absolute** and point **directly** at the tracked file in the repository: `~/.config/htop/htoprc -> ~/Dev/config/dotfiles/files/home/cbenz/.config/htop/htoprc`.
- The base-dir (where the tracked files live) is the standard `files/` subdirectory of the repository root: the app always appends `files` when resolving the base directory.
- Deployed links therefore look like `/etc/fstab -> /home/user/myfiles/files/etc/fstab`.
- There is **no indirection symlink**: if the repository root is moved or renamed, the deployed links break (they point at the old location). That is acceptable because `myfiles fix` repairs them in no time — `status` reports the broken links as `dangling`, and `fix`'s default `deploy` re-links them. Links stay absolute, readable and greppable.
- A directory can be managed as a **dir-link**: the whole directory becomes one managed symlink `dir -> base-dir/rel`, mirroring the directory in the repo. `capture <dir>` creates a dir-link; `eject` restores it (see `capture`/`eject`). Files a program writes there land in `base-dir`, so `.gitignore` (via `myfiles ignore`) is the right tool for what should never be versioned.
- A valid dir-link (a symlink pointing into an existing base-dir location) is a healthy deployed state: `status` reports nothing for it, `deploy` leaves it alone (the files are served through it), and `fix` never offers it. Only a **dangling** dir-link (its base-dir location is missing, e.g. the repository moved) or a **foreign** one is a problem: `deploy`/`fix` replace it with a real directory and link the tracked files individually.
- For regular files, links point **absolute** and **directly** at `base-dir`/`rel`; intermediate directories are real directories created with `mkdir -p` on deploy.

## Git integration

- Git is **optional**: `myfiles` works even if `base-dir` is not a Git repository.
- `myfiles` **never creates commits** — versioning is entirely left to you (stage and commit the `base-dir` changes yourself when you want to).
- The only Git interaction is a read-only safety check: `capture --force` refuses to overwrite a tracked file that has uncommitted changes, so no version is lost.

## Global options

| Option | Meaning |
| --- | --- |
| `-d`, `--base-dir <dir>` | Dotfiles repository root (e.g. `~/Dev/config/dotfiles`). The effective base-dir holding the tracked files is its `files/` subdirectory. If omitted, the `MYFILES_BASE_DIR` environment variable is used; if that is not set either, the repository root is discovered by walking up from the current directory looking for the `files/` base directory. |
| `--root-dir <dir>` | Filesystem root under which target locations are resolved (default: `/`). Useful to run the tool in a sandbox. The allowed `capture` roots are also mapped under this root (e.g. `/etc` is accepted as `<root>/etc`), so directory captures work in a sandbox. |
| `--dry-run` | Preview-only: print what would be done, without asking for confirmation and without applying anything. When active, the first line printed by any command is a `dry-run` notice. |

Base directory resolution order: `--base-dir`, then the `MYFILES_BASE_DIR` environment variable, then a `files/` base directory found in the current directory or one of its ancestors. If none is available, the command fails with an error inviting you to run it from the dotfiles repository, to pass `--base-dir`, or to set `MYFILES_BASE_DIR`.

By default (without `--dry-run`), `capture`, `deploy`, `eject` and `ignore` first print the planned changes, then ask for confirmation (`[Y/n]`) before applying them. `status` and `ls` are read-only and never ask. With `--dry-run`, every affected command starts its output with a `dry-run` notice line.

Exit codes: `0` success, `1` runtime error, `2` usage error.

## Commands

### `capture`

Moves configuration files from the system into `base-dir`, then runs `deploy` to re-create the symlinks.

```text
Usage: myfiles capture <path>... [--ignore <glob>...] [--force] [--remotes [<host>...]] [--dry-run]
```

Behavior:

- `<path>` is a file or a directory; a directory is captured as a **dir-link** (the whole directory mirrors the repo in `base-dir`).
- If `<path>` is a directory, it must be inside one of the allowed roots (see *Safety scope for `capture`*); a plain file is always accepted.
- `--ignore` is repeatable; each value is a **gitignore-style glob** matched against the path relative to `<path>` (so `cache` matches the directory at any depth, `*.log` any `.log` basename, `cache/**` everything under `cache`, a leading `/` anchors to `<path>`, a trailing `/` restricts to directories), reported as `skip (ignored)`. It only affects **directory** captures (a plain file capture is unaffected): the matched entries never count as drift, and they are **appended to the repository's `.gitignore`** (resolved to their tracked location, e.g. `~/.config/zsh/.zsh_history` -> `files/home/user/.config/zsh/.zsh_history`), so git never versions them — exactly like running `myfiles ignore` on them.
- Idempotence: if the source is **already a managed symlink** (a file symlink or a dir-link), it is skipped (not re-captured) and reported as `skip (already captured)`.
- **Directory capture (dir-link)** — `capture <dir>` makes the whole directory a single managed symlink `<dir> -> base-dir/<rel>`:
  - if `<dir>` is **fully managed** (every entry is a managed symlink correctly pointing to its tracked file, or a regular file **identical** to its tracked copy — zero drift), it is converted in place: the directory is removed and replaced by the dir-link (`convert to dir-link <dir> -> base-dir/<rel>`), nothing is moved;
  - otherwise (a fresh directory with real files) the whole directory is **moved** into `base-dir` and linked back (`move-and-link <dir> -> base-dir/<rel> (dir-link)`);
  - if `base-dir/<rel>` already holds tracked files **and** `<dir>` contains drift (a real file **whose content differs** from its tracked copy, a foreign/misplaced symlink, a managed link whose tracked file is missing), capture is **refused** unless `--force`: run `myfiles fix` to resolve the drift first, or `--force` to adopt the system content (the drifted files overwrite the tracked copies, then the directory is converted).
  - with `--ignore`, the matched entries are appended to the repository's `.gitignore` (see the `--ignore` bullet); the ones that exist on disk are moved into `base-dir` so they keep working through the dir-link.
- A symlink passed directly as `<path>` (a file symlink, not a dir-link) is resolved and its target is captured.
- Conflict (per-file capture): if a tracked file with the same relative path already exists in `base-dir`:
  - identical content → skip;
  - different content → error, unless `--force` (then it is overwritten);
  - different content + `--force` but the tracked file has uncommitted git changes → error (commit or restore it first, so no version is lost).
- Each per-file capture is reported as a single `move-and-link` operation (moved into `base-dir`, then linked back). The planned operations are printed, then confirmation is requested before applying; `--dry-run` prints them without asking or applying.

**Remote capture** — a `{host}:/path` argument (or `--remotes`) captures files from a remote host into `remotes/<host>`:

- `capture {host}:/path` copies the remote file (or, for a directory, every file under it) into `remotes/<host>/…`, **only when the content differs** (compared by SHA-256 hash); identical files are reported as `skip (identical)`. Nothing is moved and no symlink is created — the remote copy stays in place and the repo gains a copy.
- `--remotes [<host>...]` **recaptures every known remote file** of the given host(s), or of every host when no value is given: each file under `remotes/<host>` whose remote content differs is copied back from the host (same "only if differs" rule). A known file absent on the host is reported as `skip (not on remote)`. An unknown host (no `remotes/<host>` directory) is an error.
- A positional `{host}:/path` is processed even without `--remotes`; a positional path that would be swallowed by argparse's optional-value syntax after `--remotes` is recognized by its leading `/` (or `host:/` prefix) and treated as a PATH.
- `--remotes` makes `PATH` optional: `capture --remotes` alone scans every host. The remote plans are printed and confirmed like local captures (`--dry-run` previews them).

Examples:

```bash
# capture a single file
myfiles capture /etc/fstab

# capture a whole config directory, ignoring caches and logs
myfiles capture ~/.config/app --ignore cache --ignore "*.log"

# capture several paths at once
myfiles capture /etc/fstab ~/.config/app

# preview without touching the disk
myfiles capture ~/.config/app --dry-run

# capture a file from the ender3 SSH host (copied, only if it differs)
myfiles capture ender3:/home/admin/printer_data/config/printer.cfg

# recapture every known file of the ender3 host that differs on the host
myfiles capture --remotes ender3

# recapture every known remote file (all hosts)
myfiles capture --remotes
```

### `deploy`

Creates the symlinks for the tracked files in `base-dir`.

```text
Usage: myfiles deploy <path>... [--force] [--remotes [<host>...]] [--dry-run]
```

Behavior:

- A `PATH` is required unless `--remotes` is given (there is no implicit "deploy everything"). Each `PATH` is a tracked file or directory (target path or path inside the base directory); a requested path with no tracked file is reported as `skip <target>: not a tracked file`.
- **Remote deploy** — a `{host}:/path`, a path inside `remotes/`, or a root-relative `remotes/...` path deploys the tracked remote file(s) **to the host** instead: each tracked file under `remotes/<host>` is copied to the host (its parent directories are created remotely), **only when its content differs** (SHA-256). A tracked file missing on the host is uploaded. A directory path deploys every tracked file underneath it.
- **`--remotes [<host>...]`** (unified option: no value = every host) deploys **every known remote file** of the given host(s) to the host — each tracked file under `remotes/<host>` is copied only when it differs (missing remote parents are created). It makes `PATH` optional (`myfiles deploy --remotes ender3` alone deploys all of ender3). An unknown host is an error. When `--remotes` and `PATH`s are both given, the scan and the explicit paths are both processed.
- For each tracked file, the parent directories of the target location are created with `mkdir -p` if they don't exist.
- An **absolute** symlink is created from the target location to the tracked file.
- The planned symlinks are printed, then confirmation is requested before applying; `--dry-run` prints them without asking or applying.

Conflict matrix (state of the target location):

| Existing target | Action |
| --- | --- |
| nothing | create the symlink |
| managed symlink (already pointing to the tracked file) | skip (idempotence) |
| managed symlink pointing elsewhere | error, unless `--force` (then re-linked to the correct tracked file) |
| regular file with identical content | link it (replaces the file, identical content) |
| regular file with different content | error; a `diff <target> <tracked-file>` command is suggested, ready to copy-paste |
| regular file with different content + `--force` | link it (replaces the file, different content), keeping the original as `target.bak` |
| foreign symlink | error, unless `--force` (then replaced by the managed symlink) |
| directory | error |

Special case — an intermediate directory is itself a symlink into `base-dir`:

A **valid** dir-link (pointing into an existing base-dir location) is the deployed state for that directory: `deploy` leaves it alone and the tracked files underneath are served through it (`skip <target>: linked via dir-link`), so no individual symlink is created.

A **dangling** dir-link (its base-dir location is missing, e.g. the repository moved) or a **foreign** one is replaced by a real directory:

1. remove the directory symlink;
2. recreate it as a real directory;
3. link every tracked file underneath individually with an absolute symlink.

With `--force`, a **foreign** intermediate directory symlink (e.g. `~/.ssh/config.d -> <old-layout-path>`) is also replaced by a real directory and the tracked files underneath are linked individually (without `--force`, deploying under such a path is refused with `cannot deploy <target>: an intermediate path is a foreign symlink`).

Example — intermediate directories are created and files are linked:

Assuming `base-dir = /home/user/myfiles` and the following tracked files:

```text
base-dir/
├── etc/
│   └── fstab
├── home/user/
│   ├── .bashrc
│   └── .config/app/config.yaml
└── usr/local/bin/
    └── my-tool.conf
```

`myfiles deploy` creates the missing intermediate directories with `mkdir -p` (preserving those that already exist) and links each file with an **absolute** symlink pointing directly at the tracked file:

```text
/etc/fstab                          -> /home/user/myfiles/files/etc/fstab
/home/user/.bashrc                  -> /home/user/myfiles/files/home/user/.bashrc
/home/user/.config/app/config.yaml  -> /home/user/myfiles/files/home/user/.config/app/config.yaml
/usr/local/bin/my-tool.conf         -> /home/user/myfiles/files/usr/local/bin/my-tool.conf
```

Resulting layout:

```text
/etc/
└── fstab -> /home/user/myfiles/files/etc/fstab

/home/user/
├── .bashrc -> /home/user/myfiles/files/home/user/.bashrc
└── .config/app/
    └── config.yaml -> /home/user/myfiles/files/home/user/.config/app/config.yaml

/usr/local/bin/
└── my-tool.conf -> /home/user/myfiles/files/usr/local/bin/my-tool.conf
```

(Link targets are absolute and point directly at the repository; if the repository moves, `myfiles fix` re-links everything.)

### `eject`

Stops using the tool: every managed symlink is replaced by a real copy of the tracked file, and the tracked files are removed from `base-dir`.

```text
Usage: myfiles eject [<path>...] [--dry-run]
```

Behavior:

- Without arguments, the whole `base-dir` is ejected; with arguments, only the given file(s) or directory(ies).
- A `PATH` can be a **target path** (e.g. `~/.config/htop`), a **tracked path** inside the base directory (e.g. `<base-dir>/home/cbenz/.config/htop`), or a root-relative path (e.g. `etc/fstab`) — the tracked path and the target path forms are equivalent.
- The planned replacements are printed, then confirmation is requested before applying; `--dry-run` prints them without asking or applying.
- Each managed symlink (a symlink resolving into `base-dir`) is replaced by a real copy of its tracked file, reported as `move <tracked> to <target> (replacing the managed symlink)`.
- A managed **dir-link** covering the selection is restored as a whole: its tracked files are copied out into a real directory, the link is removed, and the tracked copies are deleted (`restore <dir> from base-dir/<rel> (dir-link)`).
- When the managed link is **missing** (e.g. the target file or its directory was deleted), ejection still works: the tracked content is **restored at the target** (real file, missing directories recreated) and the tracked copy removed — reported as `move <tracked> to <target>`.
- A target location that is no longer a symlink (e.g. replaced by the user with an edited file) is **left untouched** and reported.
- Tracked files are **removed from `base-dir`** after ejection. This is safe when `base-dir` is a Git repository (history keeps them), but is a permanent deletion otherwise — be careful.

Examples:

```bash
# eject everything
myfiles eject

# eject a single file
myfiles eject etc/fstab

# preview what eject would do
myfiles eject --dry-run
```

### `ignore`

Adds paths to the repository's `.gitignore`, so git never versions them (e.g. files that a program writes inside a dir-link mirroring the repo).

```text
Usage: myfiles ignore <path>... [--base-dir <dir>] [--dry-run]
```

Behavior:

- Each `PATH` is resolved to its tracked location under `files/` (e.g. `~/.config/zsh/.antidote` -> `files/home/user/.config/zsh/.antidote`).
- The resolved entries are appended to `.gitignore` at the repository root; if it does not exist, it is created with a `# managed by myfiles` header. Entries already present are skipped (`nothing to ignore`).
- The plan is printed and confirmed (`[Y/n]`); `--dry-run` prints it without applying.

Examples:

```bash
# never version a vendored clone or a rewritten history file
myfiles ignore ~/.config/zsh/.antidote ~/.config/htop/htop_history
```

### `status`

Reports the current state without modifying anything.

```text
Usage: myfiles status [<path>...] [--remotes [<host>...]]
```

Without arguments, `status` reports on every tracked file. When one or more `PATH` are given, it reports only on those files — a `PATH` can be a target path (e.g. `~/.config/Thunar/uca.xml`) or a path inside the base directory. A requested file that is not tracked is reported as `not tracked`.

With `--remotes [<host>...]` (unified option: no value = every host), `status` reports the **remote** state instead: the known files under `remotes/` of the given host(s) — or of every host — are compared with their copies on the host. It prints only the differences:

- `drift` — the remote file exists but differs from its tracked copy; the two modification dates are shown (`local:`/`remote:`) and the **most recent side is marked `(newer)`**;
- `missing` — the tracked file is absent on the host.

Identical remote files are hidden. When at least one difference is found, `status --remotes` ends with a hint inviting to run `myfiles fix --remotes`. Exit code is `1` if any difference is found, `0` otherwise.

`status` prints **only problems**; healthy managed symlinks and valid dir-links are hidden. Each line starts with a short reason word:

- `dangling` — a foreign symlink whose target is missing;
- `foreign` — a symlink not managed by `myfiles`; a foreign intermediate directory symlink is reported **once** (as `foreign`, or `dangling` if its target is missing) instead of per-file;
- `elsewhere` — a managed symlink pointing to a different tracked file;
- `directory` — the target is a directory instead of a symlink;
- `not-linked` — a regular file with identical content (not yet a symlink);
- `drift` — a regular file whose content differs from the tracked file;
- `missing` — the target does not exist.

For `dangling`, `foreign` and `elsewhere`, the symlink's target is shown after `->` (e.g. `dangling /etc/nsswitch.conf -> ~/Dev/config/dotfiles/global/etc/nsswitch.conf`).

When at least one problem is detected, `status` ends with a blank line and a hint inviting to run `myfiles fix` (e.g. `hint: run 'myfiles fix' to resolve these problems interactively`).

Exit code is `1` if any problem is detected (any of the above), `0` otherwise.

### `ls`

Lists the files tracked by `myfiles` (target locations, with a leading `/`), one per line, honoring the repository's `.gitignore`.

```text
Usage: myfiles ls [--remotes [<host>...]] [--base-dir <dir>] [--root-dir <dir>]
```

Behavior:

- Each tracked file is printed sorted, one per line, as its **target location** (e.g. `/etc/fstab`, `/home/user/.bashrc`) — the path under `--root-dir` (default `/`).
- Files matching the repository's `.gitignore` (e.g. the transient files written inside a dir-link and gitignored with `myfiles ignore`) are **not listed**: `ls` shows only what git actually versions.
- Prints nothing (and exits `0`) when nothing is listed.
- It is read-only and never asks; the base-dir is resolved as usual (`--base-dir`, `MYFILES_BASE_DIR`, or discovery from the current directory).
- With `--remotes [<host>...]` (unified option: no value = every host), the tracked **remote** files are listed instead, as `host:/path` (e.g. `ender3:/home/admin/printer_data/config/printer.cfg`); the repository's `.gitignore` still applies (`remotes/<host>/…` entries are hidden).

Examples:

```bash
# list everything versioned by myfiles
myfiles ls

# list the tracked files of the ender3 remote host
myfiles ls --remotes ender3

# feed a list back into the tool
myfiles deploy $(myfiles ls)
```

### `fix`

Interactively resolves the problems reported by `status`, one by one. It lists each problem (same labels as `status`) and asks what to do with it.

```text
Usage: myfiles fix [<path>...] [--defaults] [--only <errtype>...] [--remotes [<host>...]] [--base-dir <dir>] [--dry-run] [--root-dir <dir>]
```

Without arguments, `fix` offers **every** problem reported by `status`. With one or more `PATH`, only the problems of the given file(s)/directory(ies) are processed (a `PATH` can be a target path, a tracked path inside the base directory, or a root-relative path — like `eject`/`deploy`).

- With `--only <errtype>` (repeatable), only the problems of the given type(s) are processed — the same labels as `status`: `dangling`, `foreign`, `elsewhere`, `not-linked`, `missing`, `drift`, `directory`. An unknown type is rejected by the CLI (exit `2`).
- `--defaults` runs **non-interactively**: every problem is fixed with its default action (no choice, no confirmation). Problems without a default (`drift`) and non-auto-fixable ones (`directory`) are skipped and reported.

For each problem, the available actions depend on its type, and there is a safe default (tracked files are the authority — `deploy` is never destructive of tracked content):

| Problem | Available actions | Default |
| --- | --- | --- |
| `dangling`, `foreign`, `elsewhere`, `not-linked`, `missing`, foreign/dangling directory symlink | `deploy`, `skip` | `deploy` |
| `drift` | `deploy`, `capture`, `diff`, `skip` | *(none — an explicit choice is required)* |
| `directory` | `skip` | `skip` (not auto-fixable, reported only) |

Behavior:

- For `dangling`/`foreign`/`elsewhere`/`not-linked`/`missing`, `deploy` re-links the tracked file (replacing the conflicting symlink or file, like `deploy --force`).
- For a dangling/foreign directory symlink problem, one `deploy` replaces the directory symlink with a real directory and links every tracked file underneath it. A valid dir-link is not a problem and is never offered.
- For `drift`, `deploy` replaces the system file with the tracked one (the tracked copy is the authority, the drifted system file is kept as `target.bak`); `capture` moves the system file into `base-dir` (becoming the new tracked copy) and links it back; `diff` runs `diff -Naur <tracked> <target>` and re-asks; `skip` leaves it alone.
- A problem is kept only if it concerns one of the requested `PATH`s: a file is matched by its exact tracked path, a directory by everything underneath it (so fixing a directory-symlink problem deploys all its files). If no problem matches the selection, `fix` prints `no problems to fix`.
- The answers are `d`/`deploy`, `c`/`capture`, `i`/`diff` (inspect), `s`/`skip`; empty input picks the default when there is one. `drift` has **no default**: an empty or invalid answer re-asks.
- Each chosen change is **confirmed and applied immediately**, item by item (the items are independent): its plan is printed, then `[Y/n]` is asked exactly like running `deploy`/`capture` by hand, and the change is applied on `Y` (`N change(s) applied`) or left untouched on `n`. With `--dry-run` the plan is only previewed (no confirmation). There is no batch plan/confirmation at the end.
- Interrupting the session (`Ctrl-C`/EOF) — at either the choice or the confirmation prompt — aborts the whole `fix` (it does not move to the next item), prints `aborted (changes already applied are kept)` and exits with code `130` — items already applied before the interruption are kept.
- There is deliberately no `fix --all`: every change is chosen, confirmed and applied individually.

**Remote fix (`--remotes [<host>...]`)** — with `--remotes` (unified option: no value = every host), `fix` resolves the remote differences (those reported by `status --remotes`) of the given host(s) instead of the local problems, with the **same REPL**:

- Each differing file is offered as `drift` (remote exists but differs) or `missing` (tracked file absent on the host), labelled with its `{host}:/path`.
- For `drift`, the actions are `deploy` (copy the tracked file to the host, the tracked copy is the authority), `capture` (copy the host file into the repo, the host content becomes the new tracked copy), `diff` (show the difference, downloading the remote file into a temporary directory) and `skip` — **no default** (an explicit choice is required), exactly like local `drift`.
- For `missing`, the actions are `deploy` (upload the tracked file to the host, the default) and `skip`.
- Each chosen change is confirmed and applied immediately, item by item, like local `fix`; `--defaults` applies the default action of every problem (`drift` has no default and is skipped); `Ctrl-C` aborts the whole session (already-applied items are kept).

### `diff`

Shortcut to compare a system file with its tracked copy, equivalent to `diff -Naur <tracked> <target>` — the tracked copy (in the base directory) is the `before` side, the system file is the `after` side.

```text
Usage: myfiles diff <path> [--base-dir <dir>] [--root-dir <dir>]
```

Behavior:

- `<path>` is either:
  - a **system target path** (e.g. `/etc/UPower/UPower.conf`) — it is compared with `<base-dir>/etc/UPower/UPower.conf`;
  - a **tracked path** inside the base directory (e.g. `<base-dir>/etc/UPower/UPower.conf`) — it is compared with `/etc/UPower/UPower.conf` (under `--root-dir`).
  - a **remote path** `{host}:/path` (or a tracked path inside `remotes/`) — the tracked remote file is compared with the remote copy, which is **first downloaded into a temporary directory**; the two sides are labelled `<tracked>` and `{host}:/path`.
- The comparison uses the system `diff` command with `-Naur` (missing files are treated as empty): the tracked copy is the `before` side, the system file the `after` side.
- Exit code is the `diff` exit code: `0` if identical, `1` if differences, `2` on error.

Examples:

```bash
myfiles diff /etc/UPower/UPower.conf
myfiles diff /path/to/base-dir/etc/UPower/UPower.conf
```

## Non-goals

- Multi-machine / multi-host synchronization.
- Encryption of the stored files.
- Windows support (symlinks and POSIX semantics).

## Acceptance criteria (illustrative)

| Scenario | Expected result |
| --- | --- |
| `capture /etc/fstab` when nothing exists | file moved to `base-dir/etc/fstab`, symlink created at `/etc/fstab` |
| capturing the same path twice | second run is a no-op |
| `capture ~/.bashrc` | accepted (only directories are restricted); moved to `base-dir/home/user/.bashrc`, symlink created at `~/.bashrc` |
| `deploy` when a regular file with identical content exists at the target | file replaced by a symlink |
| `deploy` when a regular file with different content exists at the target | error + suggested `diff` command, no change without `--force` |
| `deploy` when `/etc/foo` is a valid symlink into `base-dir` and `base-dir/etc/foo/bar.txt` is tracked | the dir-link is left alone, `bar.txt` served through it (no individual link) |
| `deploy` when `/etc/foo` is a dangling/foreign directory symlink | replaced by a real directory, the tracked files underneath linked individually |
| `eject` on a managed system | every managed symlink becomes a real file, content identical, files removed from `base-dir` |
| `capture`/`deploy`/`eject` without `--dry-run` | the plan is printed and confirmation (`[Y/n]`) is requested before any change |
| `myfiles status` when not run from the repository and without `--base-dir` | error inviting to run the command from the dotfiles repository or pass `--base-dir` |
| `myfiles status` run from inside the repository | the base-dir is discovered via the `files/` directory (no `--base-dir` needed) |
| `myfiles ls` | prints each versioned tracked file as a target path with a leading `/` (gitignored files are hidden) |
| `deploy --root-dir /tmp/sandbox` | symlinks are created under `/tmp/sandbox` instead of `/` |
| `myfiles diff /etc/UPower/UPower.conf` | same as `diff -Naur <base-dir>/etc/UPower/UPower.conf /etc/UPower/UPower.conf` |
| `status` when an intermediate directory of a target is a valid symlink into `base-dir` | healthy: nothing reported, exit code `0` |
| `capture <dir>` when the directory is fully managed (managed symlinks, or regular files identical to their tracked copy) | converted to a dir-link: the directory removed, `<dir> -> base-dir/<rel>` created |
| `capture <dir>` when the directory contains drift (a file differing from its tracked copy) | refused (exit `1`) unless `--force` (then the system content wins) |
| `capture <dir> --ignore X` | the directory is captured as a dir-link and `X` is appended to the repo's `.gitignore` (moved into `base-dir`, so it keeps working and is never versioned) |
| `eject` on a managed dir-link | the whole directory restored as real files, the link and tracked copies removed |
| `--dry-run` on any command | no filesystem change |
| `myfiles ignore ~/.config/zsh/.antidote` | appends `files/home/user/.config/zsh/.antidote` to `.gitignore` (created with a managed-by-myfiles header) |
| `myfiles ignore <path>` when the entry is already there | prints `nothing to ignore`, no change |
| `capture --root-dir /tmp/sandbox` of a directory under the sandbox | allowed: `/etc`-like roots are mapped under the sandbox |
| `myfiles deploy` without any `PATH` | argparse error (paths are required), exit code `2` |
| `myfiles fix` when the status is clean | prints `no problems to fix`, exit code `0` |
| `myfiles fix` on a `dangling`/`foreign` symlink | default `deploy` replaces it with the managed link |
| `myfiles fix` on a `drift` file, choosing `capture` | the system file becomes the tracked copy, linked back in place |
| `myfiles fix` on a `drift` file, pressing Enter (no choice) | re-asks (no default), nothing is changed |
| `myfiles fix` on a foreign directory symlink | the symlink is replaced by a real directory and the files underneath are linked |
| `myfiles fix --only dangling` | only the `dangling` problems are processed, the others are left untouched |
| `myfiles fix --defaults` | applies the default action of each problem without asking; `drift` (no default) is skipped |
| `myfiles fix --only bogus` | CLI rejects the unknown type, exit code `2` |
| `myfiles fix --dry-run` | prints the plan without applying |
