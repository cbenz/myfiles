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
        "paths", nargs="+", metavar="PATH", help="File(s) or directory(ies) to capture."
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
        help="Overwrite an existing tracked file with different content.",
    )

    deploy_parser = sub.add_parser(
        "deploy", help="Create symlinks for the tracked files."
    )
    add_base_dir(deploy_parser)
    add_dry_run(deploy_parser)
    add_root_dir(deploy_parser)
    deploy_parser.add_argument(
        "paths",
        nargs="+",
        metavar="PATH",
        help="File(s) or directory(ies) to deploy (required).",
    )
    deploy_parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Replace a conflicting target file even if it differs.",
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
        help="Interactively resolve the status problems (deploy or capture per item).",
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
        help="Non-interactive: apply the default action of every problem (drift is skipped: it has no default).",
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

    diff_parser = sub.add_parser(
        "diff",
        help="Show differences between a system file and its tracked copy (like `diff -Naur`).",
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

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "capture":
        return capture(
            args.base_dir,
            args.paths,
            args.ignore,
            args.force,
            args.dry_run,
            args.root_dir,
        )
    if args.command == "deploy":
        return deploy(
            args.base_dir, args.paths, args.force, args.dry_run, args.root_dir
        )
    if args.command == "eject":
        return eject(args.base_dir, args.paths, args.dry_run, args.root_dir)
    if args.command == "status":
        return status(args.base_dir, args.paths, args.root_dir)
    if args.command == "ignore":
        return ignore(args.base_dir, args.paths, args.dry_run, args.root_dir)
    if args.command == "diff":
        return diff(args.base_dir, args.path, args.root_dir)
    if args.command == "ls":
        return ls(args.base_dir, args.root_dir)
    if args.command == "fix":
        return fix(
            args.base_dir,
            args.dry_run,
            args.paths,
            args.root_dir,
            args.defaults,
            args.only,
        )
    return 0
