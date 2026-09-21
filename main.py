"""Entry point for epub-font-unlock.

    python main.py                                          -> GUI
    python main.py book.epub --font --size --line-height    -> CLI

There is no --password option: an EPUB we cannot read is a DRM-protected one,
and those are reported and skipped rather than opened. Nothing here asks a
question either, so there is no -y/--yes; a run either happens or it does not.
"""
from __future__ import annotations

import argparse
import os
import sys

import i18n
import unlock
from i18n import t


def _preselect_lang(argv: list[str]) -> str | None:
    """--lang has to be known before the parser is built, so help is translated."""
    for i, a in enumerate(argv):
        if a == "--lang" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--lang="):
            return a.split("=", 1)[1]
    return None


def _localize_argparse() -> None:
    """argparse's own labels go through gettext; route them to lang files."""
    table = {
        "usage: ": t("cli_usage"),
        "positional arguments": t("cli_positional"),
        "options": t("cli_options"),
        "show this help message and exit": t("cli_help"),
    }
    argparse._ = lambda s: table.get(s, s)  # type: ignore[attr-defined]


def build_parser() -> argparse.ArgumentParser:
    _localize_argparse()
    p = argparse.ArgumentParser(prog="epub-font-unlock", description=t("cli_desc"))
    p.add_argument("inputs", nargs="*", help=t("cli_inputs"))
    # --- TOOL OPTIONS ---------------------------------------------------
    p.add_argument("--font", action="store_true", help=t("cli_font"))
    p.add_argument("--size", action="store_true", help=t("cli_size"))
    p.add_argument("--line-height", dest="line", action="store_true", help=t("cli_line"))
    p.add_argument("--margin", action="store_true", help=t("cli_margin"))
    p.add_argument("--remove-font-files", dest="remove_font_files", action="store_true",
                   help=t("cli_remove_files"))
    p.add_argument("--analyze-only", dest="analyze_only", action="store_true",
                   help=t("cli_analyze_only"))
    # --- END TOOL OPTIONS -----------------------------------------------
    p.add_argument("--lang", choices=i18n.LANGS, help=t("cli_lang"))
    p.add_argument("--gui", action="store_true", help=t("cli_gui"))
    return p


def options_from(args: argparse.Namespace) -> unlock.Options:
    """With no unlock flags given, fall back to the three the GUI ticks by
    default. Naming any flag means the user is choosing the whole set."""
    if not any((args.font, args.size, args.line, args.margin, args.remove_font_files)):
        return unlock.Options().validated()
    return unlock.Options(font=args.font, size=args.size, line=args.line,
                          margin=args.margin,
                          remove_font_files=args.remove_font_files).validated()


def _print_analysis(counts: unlock.Counts) -> None:
    print("  " + t("analysis_summary", fonts=counts.family_count,
                   sizes=counts.sizes, lines=counts.lines))
    print("  " + t("analysis_extra", margins=counts.margins, inline=counts.inline,
                   font_files=counts.font_files))


def run_cli(args: argparse.Namespace) -> int:
    files = unlock.collect_epubs(args.inputs)
    for p in args.inputs:
        if not os.path.exists(p):
            print(t("err_open_failed", name=p), file=sys.stderr)
    if not files:
        print(t("cli_no_input"), file=sys.stderr)
        return 2
    opts = options_from(args)
    failures = processed = skipped = 0
    for index, path in enumerate(files, 1):
        name = os.path.basename(path)
        print(t("cli_processing", index=index, total=len(files), name=name))
        try:
            if args.analyze_only:
                _print_analysis(unlock.analyze_epub(path).counts)
                processed += 1
                continue
            result = unlock.process_epub(path, opts)
        except unlock.DrmProtected:
            print("  " + t("err_drm"), file=sys.stderr)
            print("  " + t("log_drm", name=name))
            skipped += 1
            continue
        except unlock.BrokenArchive:
            print("  " + t("err_open_failed", name=name), file=sys.stderr)
            failures += 1
            continue
        except KeyboardInterrupt:
            print("  " + t("status_cancelled"))
            return 130
        except Exception as exc:  # one bad book must not end the batch
            print("  " + t("err_file_failed", name=name, error=exc), file=sys.stderr)
            failures += 1
            continue
        for item, error in result.failed:
            print("  " + t("err_css_failed", name=item, error=error), file=sys.stderr)
        if result.already_unlocked:
            print("  " + t("msg_already"))
            print("  " + t("log_already", name=name))
            skipped += 1
            continue
        counts = result.counts
        print("  " + t("msg_changes", fonts=counts.fonts, sizes=counts.sizes,
                       lines=counts.lines, margins=counts.margins))
        print("  " + t("log_saved", path=result.output_path))
        processed += 1
    print(t("msg_done", count=processed))
    if skipped:
        print(t("msg_skipped", count=skipped))
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Windows consoles default to cp949; Chinese/Japanese file names would crash print().
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
            except Exception:
                pass
    i18n.init(_preselect_lang(argv))
    args = build_parser().parse_args(argv)
    if args.lang:
        i18n.set_lang(args.lang, persist=False)
    # A windowed exe has no console, so dropping files on it opens the GUI
    # with those files loaded instead of running the CLI into nowhere.
    headless = getattr(sys, "frozen", False) and sys.stdout is None
    if args.gui or headless or not args.inputs:
        import gui

        gui.launch(args.inputs or None)
        return 0
    return run_cli(args)


if __name__ == "__main__":
    sys.exit(main())
