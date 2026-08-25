# myfiles

Version-controls a machine's configuration files with Git, linking them through **symlinks** — file by file or via **dir-links** (a whole directory becomes a single symlink).

## Why myfiles?

Instead of chezmoi, dotdrop or yadm:

- **Symlinks, always.** Every tracked file is deployed as an **absolute** symlink pointing straight into the repository (`~/.config/htop/htoprc -> ~/Dev/config/dotfiles/files/home/cbenz/.config/htop/htoprc`). No copies, no templates: what's in the repo is exactly what's used. (chezmoi copies by default; dotdrop and yadm manage alternate views or profiles.)
- **Dir-links are allowed.** A directory can be managed as one unit: `~/.config/zsh -> files/home/cbenz/.config/zsh`. Anything a program writes there lands directly in the repo — essential for programs that rewrite their files atomically (gitk, htop) and would replace a plain file symlink.
- **No DSL, no git wrapper.** myfiles is neither a git wrapper (yadm) nor a templating/profiles engine (chezmoi, dotdrop): just a `files/` layout mirroring the system, and you run `git commit` yourself.
- **Safety first.** No data loss: conflicts are refused without `--force`, and `eject` guarantees you are never locked in.
- **Simple and readable.** No config file: what git versions is exactly the contents of the `files/` directory, and `myfiles ignore` fills in `.gitignore`.

## How it works

The **base-dir** (`files/`) mirrors the system: the leading `/` of a target path is stripped.

| Target | Tracked file |
| --- | --- |
| `/etc/fstab` | `files/etc/fstab` |
| `~/.bashrc` | `files/home/cbenz/.bashrc` |
| `~/.config/app/config.yaml` | `files/home/cbenz/.config/app/config.yaml` |

- **Symlink**: `~/.config/htop/htoprc -> ~/Dev/config/dotfiles/files/home/cbenz/.config/htop/htoprc`.
- **Dir-link**: a whole directory is a symlink (`~/.config/zsh -> .../files/home/cbenz/.config/zsh`); files programs write there land in the repo and are git-ignored via `myfiles ignore`.
- **Git**: myfiles never creates commits — version `files/` yourself.

## Installation

Requires Python 3.14+.

```bash
uv sync
uv run myfiles --help
```

`myfiles` is exposed as a console script; with the environment activated (`.venv/bin/activate`), plain `myfiles` works.

To run `myfiles` from anywhere:

```bash
export MYFILES_BASE_DIR=~/Dev/config/dotfiles
```

Repository resolution order: `--base-dir` > `MYFILES_BASE_DIR` > a `files/` directory found walking up from the current directory.

## Usage

Global options:

| Option | Purpose |
| --- | --- |
| `-d, --base-dir <dir>` | Dotfiles repository root (contains `files/`). Default: `MYFILES_BASE_DIR`, otherwise discovered from the current directory. |
| `--root-dir <dir>` | Filesystem root under which targets are resolved (default `/`) — useful to test in a sandbox. |
| `--dry-run` | Preview only: print what would be done, without applying or asking. |

Exit codes: `0` success, `1` runtime error, `2` usage error.

### capture

Moves files from the system into `base-dir`, then (re)creates the symlinks.

```
myfiles capture <path>... [--ignore <glob>...] [--force] [--dry-run]
```

- A **file** is moved into `base-dir` and linked.
- A **directory** is captured as a **dir-link**: the whole directory becomes a single symlink.
- `--ignore <glob>` (repeatable, gitignore syntax) appends the matched entries to the repository's `.gitignore` — for transient files (history, caches, vendored clones) that should never be versioned.
- `--force`: on drift, the system content becomes the new tracked copy.

```bash
# a single file
myfiles capture /etc/fstab

# a whole directory as a dir-link, ignoring transient files
myfiles capture ~/.config/zsh --ignore .zsh_history --ignore .antidote

# preview
myfiles capture ~/.config/app --dry-run
```

### deploy

Creates the symlinks for the tracked files.

```
myfiles deploy <path>... [--force] [--dry-run]
```

- At least one `PATH` is required (no implicit "deploy everything"). A `PATH` can be a target path, a tracked path, or a root-relative path.
- A conflict (different file, foreign symlink, directory) is refused without `--force`.

```bash
myfiles deploy etc/fstab ~/.config/htop
```

### eject

Replaces every managed symlink with a real copy and removes the tracked files — the system behaves as if myfiles had never been used.

```
myfiles eject [<path>...] [--dry-run]
```

```bash
# everything
myfiles eject
# a single file
myfiles eject etc/fstab
```

### status

Reports problems, without modifying anything.

```
myfiles status [<path>...]
```

Prints only problems: `dangling`, `foreign`, `elsewhere`, `directory`, `not-linked`, `drift`, `missing`. Healthy symlinks and valid dir-links are hidden. Exit code `1` if any problem is detected.

```bash
myfiles status
```

### fix

Resolves problems interactively, one by one.

```
myfiles fix [<path>...] [--defaults] [--only <errtype>...] [--dry-run]
```

- `--only <errtype>` (repeatable): only resolve that problem type (`dangling`, `drift`, …).
- `--defaults`: non-interactive — applies each problem's default action; `drift` (no default) is skipped.
- Each fix is confirmed and applied immediately; `Ctrl-C` aborts the session (exit code `130`), keeping the already-applied fixes.

```bash
# everything, interactively
myfiles fix
# only the drifted files
myfiles fix --only drift
# apply the defaults without asking
myfiles fix --defaults
```

### diff

Compares a system file with its tracked copy (like `diff -Naur`).

```
myfiles diff <path>
```

```bash
myfiles diff /etc/UPower/UPower.conf
```

### ignore

Adds paths to the repository's `.gitignore` (resolved under `files/`), so they are never versioned.

```
myfiles ignore <path>... [--dry-run]
```

```bash
myfiles ignore ~/.config/zsh/.antidote ~/.config/htop/htop_history
```

### ls

Lists the files versioned by myfiles (target paths with a leading `/`), one per line, honoring `.gitignore`.

```
myfiles ls
```

```bash
myfiles ls
# /etc/fstab
# /home/cbenz/.bashrc

# feed the list back in
myfiles deploy $(myfiles ls)
```

## Development

```bash
just fix    # format + auto-fix lint
just check  # format-check + lint + type-check
just test   # tests
```
