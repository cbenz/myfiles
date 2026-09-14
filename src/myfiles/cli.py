"""Command-line interface for myfiles."""

import argparse

from myfiles.commands import (
    capture,
    deploy,
    diff,
    eject,
    fix,
    ignore,
    ls,
    status,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="myfiles",
        description="Synchronize configuration files with a Git-managed directory using symlinks.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_base_dir(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument(
            "-d",
            "--base-dir",
            default=None,
            help="Base directory (the dotfiles repository root) holding the tracked files; defaults to the MYFILES_BASE_DIR environment variable, then to discovery from the current directory.",
        )

    def add_dry_run(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument(
            "-n",
            "--dry-run",
            action="store_true",
            help="Preview-only: print what would be done, without asking for confirmation or applying anything.",
        )

    def add_root_dir(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument(
            "--root-dir",
            default="/",
            help="Filesystem root under which target locations are resolved (default: /). Useful to run in a sandbox.",
        )

    capture_parser = sub.add_parser(
        "capture", help="Move files into the base directory, then deploy symlinks."
    )
    add_base_dir(capture_parser)
    add_dry_run(capture_parser)
    add_root_dir(capture_parser)
    capture_parser.add_argument(
        "paths",
        nargs="*",
        metavar="PATH",
        help="File(s) or directory(ies) to capture (or `host:/path` for a remote file/directory). Required unless --remotes is given.",
    )
    capture_parser.add_argument(
        "--ignore",
        action="append",
        default=[],
        metavar="GLOB",
        help="Glob to ignore (repeatable), matched against the path relative to PATH.",
    )
    capture_parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Overwrite an existing tracked file with different content (skipped when that file has uncommitted changes: commit it first, the commit is the backup).",
    )
    capture_parser.add_argument(
        "--remotes",
        nargs="*",
        default=None,
        metavar="HOST",
        help="Recapture the known files of the given remote host(s), or of every host when no value is given (each differing file is copied back from the host). A `host:/path` argument captures a specific remote file/directory.",
    )

    deploy_parser = sub.add_parser(
        "deploy", help="Create symlinks for the tracked files."
    )
    add_base_dir(deploy_parser)
    add_dry_run(deploy_parser)
    add_root_dir(deploy_parser)
    deploy_parser.add_argument(
        "paths",
        nargs="*",
        metavar="PATH",
        help="File(s) or directory(ies) to deploy (target path, tracked path, `host:/path` or `remotes/...`). Required unless --remotes is given.",
    )
    deploy_parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Replace a conflicting target file even if it differs (the original is kept as <target>.bak; on a host, as <remote path>.bak).",
    )
    deploy_parser.add_argument(
        "--remotes",
        nargs="*",
        default=None,
        metavar="HOST",
        help="Deploy the tracked files of the given remote host(s) to the host, or of every host when no value is given (each differing file is copied, only if it differs).",
    )

    eject_parser = sub.add_parser(
        "eject",
        help="Replace managed symlinks with real copies and remove tracked files.",
    )
    add_base_dir(eject_parser)
    add_dry_run(eject_parser)
    add_root_dir(eject_parser)
    eject_parser.add_argument(
        "paths",
        nargs="*",
        metavar="PATH",
        help="File(s) or directory(ies) to eject (default: all).",
    )

    status_parser = sub.add_parser(
        "status", help="Report the state of the tracked files."
    )
    add_base_dir(status_parser)
    add_root_dir(status_parser)
    status_parser.add_argument(
        "paths",
        nargs="*",
        metavar="PATH",
        help="Restrict the report to the given file(s) (target path or tracked path).",
    )
    status_parser.add_argument(
        "--remotes",
        nargs="*",
        default=None,
        metavar="HOST",
        help="Report the remote files that differ from their tracked copies instead of the local problems (given host(s), or every host when no value).",
    )

    ignore_parser = sub.add_parser(
        "ignore",
        help="Add paths to the repository's .gitignore (resolved under files/).",
    )
    add_base_dir(ignore_parser)
    add_dry_run(ignore_parser)
    add_root_dir(ignore_parser)
    ignore_parser.add_argument(
        "paths", nargs="+", metavar="PATH", help="File(s)/directory(ies) to ignore."
    )

    fix_parser = sub.add_parser(
        "fix",
        help="Interactively resolve the status problems (for a drift, the most recently modified side is proposed by default: capture if the system/remote file is newer, deploy if the tracked file is).",
    )
    add_base_dir(fix_parser)
    add_dry_run(fix_parser)
    add_root_dir(fix_parser)
    fix_parser.add_argument(
        "paths",
        nargs="*",
        metavar="PATH",
        help="Only resolve the problems of the given file(s)/directory(ies) (default: all).",
    )
    fix_parser.add_argument(
        "--defaults",
        action="store_true",
        help="Non-interactive: apply the default action of every problem (for a drift, the most recently modified side wins: capture if the system/remote file is newer, deploy if the tracked file is; equal/unknown dates are skipped).",
    )
    fix_parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="ERRTYPE",
        choices=[
            "dangling",
            "foreign",
            "elsewhere",
            "not-linked",
            "missing",
            "drift",
            "directory",
        ],
        help="Only fix the problems of this type (repeatable).",
    )
    fix_parser.add_argument(
        "--remotes",
        nargs="*",
        default=None,
        metavar="HOST",
        help="Fix the remote differences instead of the local problems (given host(s), or every host when no value; deploy/capture/diff per file, same REPL as local drift).",
    )

    diff_parser = sub.add_parser(
        "diff",
        help="Show differences between a system file and its tracked copy (`diff -Naur`), the newer file shown as `+`.",
    )
    add_base_dir(diff_parser)
    add_root_dir(diff_parser)
    diff_parser.add_argument(
        "path",
        metavar="PATH",
        help="System target path (e.g. /etc/...) or a tracked path inside the base directory.",
    )

    ls_parser = sub.add_parser(
        "ls", help="List the versioned tracked files (one target path per line)."
    )
    add_base_dir(ls_parser)
    add_root_dir(ls_parser)
    ls_parser.add_argument(
        "--remotes",
        nargs="*",
        default=None,
        metavar="HOST",
        help="List the tracked files of the given remote host(s) as `host:/path` (or every host when no value).",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "capture" and not args.paths and args.remotes is None:
        parser.error("capture requires at least one PATH or --remotes")
    if args.command == "deploy" and not args.paths and args.remotes is None:
        parser.error("deploy requires at least one PATH or --remotes")
    if args.command == "capture":
        return capture(
            args.base_dir,
            args.paths,
            args.ignore,
            args.force,
            args.dry_run,
            args.root_dir,
            remotes=args.remotes,
        )
    if args.command == "deploy":
        return deploy(
            args.base_dir,
            args.paths,
            args.force,
            args.dry_run,
            args.root_dir,
            remotes=args.remotes,
        )
    if args.command == "eject":
        return eject(args.base_dir, args.paths, args.dry_run, args.root_dir)
    if args.command == "status":
        return status(args.base_dir, args.paths, args.root_dir, args.remotes)
    if args.command == "ignore":
        return ignore(args.base_dir, args.paths, args.dry_run, args.root_dir)
    if args.command == "diff":
        return diff(args.base_dir, args.path, args.root_dir)
    if args.command == "ls":
        return ls(args.base_dir, args.root_dir, args.remotes)
    if args.command == "fix":
        return fix(
            args.base_dir,
            args.dry_run,
            args.paths,
            args.root_dir,
            args.defaults,
            args.only,
            args.remotes,
        )
    return 0
