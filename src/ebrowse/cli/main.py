"""ebrowse CLI entry point.

Thin client per docs/architecture.md: parse argv, ensure daemon, send one request, print
response. No page logic lives here. Help text is written for LLM agents: short,
example-first, ≤6 lines per verb.
"""

from __future__ import annotations

import argparse
import sys

OPERATING_LOOP = """\
loop: open URL -> outline (skim sections) -> expand <sid> (read one section)
      -> act on @refs (click/fill/...) -> read the returned diff
      -> re-outline only after navigation or when confused"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ebrowse",
        description="Token-efficient browser control for agents.\n" + OPERATING_LOOP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--session", default="default", help="named session (default: default)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--timeout", type=int, default=None, metavar="MS", help="command timeout")
    p.add_argument("--quiet", action="store_true", help="suppress non-essential output")
    sub = p.add_subparsers(dest="verb", metavar="<verb>")

    def verb(name: str, help_: str, aliases: list[str] | None = None) -> argparse.ArgumentParser:
        return sub.add_parser(
            name,
            help=help_,
            aliases=aliases or [],
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )

    # --- Navigation & lifecycle ---
    v = verb(
        "open",
        "open URL (launches browser if needed). ex: ebrowse open example.com",
        aliases=["goto"],
    )
    v.add_argument("url")

    verb("back", "history back; prints diff/outline")
    verb("forward", "history forward")
    verb("reload", "reload current page")

    v = verb("close", "close session's browser context")
    v.add_argument("--all", action="store_true", help="close every session")

    verb("tabs", "list open tabs")
    v = verb("tab", "switch active tab. ex: ebrowse tab 2")
    v.add_argument("index", type=int)

    v = verb(
        "dialog",
        "resolve a native confirm/prompt blocking the page. "
        'ex: ebrowse dialog accept | ebrowse dialog accept "Jane" | ebrowse dialog dismiss',
    )
    v.add_argument("response", choices=["accept", "dismiss", "status"])
    v.add_argument("text", nargs="?", help="answer text for a prompt (accept only)")

    v = verb("connect", "attach to running Chrome over CDP. ex: ebrowse connect 9222")
    v.add_argument("target", help="port or ws:// CDP url")

    v = verb("daemon", "daemon control")
    v.add_argument("action", choices=["status", "stop"])

    verb("doctor", "check install, browser, summarizer; prints fix hints")
    v = verb("mcp", "run an MCP stdio server exposing the browser tools")
    v.add_argument("--mcp-session", default="mcp", help="daemon session name to use")

    # --- Observation ---
    v = verb("outline", "sectioned page overview (the default way to look at a page)")
    v.add_argument("--refresh", action="store_true", help="force re-observation")
    v.add_argument("--wait-summaries", action="store_true", help="block until LLM labels ready")
    v.add_argument("--no-summaries", action="store_true", help="deterministic labels only")
    v.add_argument(
        "--preview",
        action="store_true",
        help="append a short verbatim text preview after each ≈ summary",
    )

    v = verb("expand", "full content of one section as markdown with @refs. ex: ebrowse expand s3")
    v.add_argument("target", help="section id (s3) or element ref (@e5)")
    v.add_argument("--cursor", type=int, default=0, help="list offset for long sections")
    v.add_argument("--all", action="store_true", help="no pagination (may be large)")

    v = verb("screenshot", "PNG of viewport/section/element. ex: ebrowse screenshot --section s3")
    v.add_argument("-o", "--output", default=None, help="output path (default: temp file)")
    v.add_argument("--section", default=None, help="clip to section bbox")
    v.add_argument("--ref", default=None, help="clip to element bbox")
    v.add_argument("--full", action="store_true", help="full page height")

    v = verb("get", "small getters. ex: ebrowse get value @e3 | ebrowse get url")
    v.add_argument("what", choices=["text", "value", "attr", "title", "url", "html"])
    v.add_argument("target", nargs="?", help="@ref or CSS selector")
    v.add_argument("attr", nargs="?", help="attribute name (for 'attr')")

    # --- Actions (all print a diff of what changed) ---
    v = verb("click", "click element; prints what changed. ex: ebrowse click @e12")
    v.add_argument("target", help="@ref or CSS selector")
    v.add_argument("--double", action="store_true")
    v.add_argument("--right", action="store_true")
    v.add_argument("--new-tab", action="store_true", help="open link in new tab")

    v = verb("fill", 'clear + type. ex: ebrowse fill @e3 "hello@example.com"')
    v.add_argument("target")
    v.add_argument("text")

    v = verb("type", "type into element without clearing. --enter to submit")
    v.add_argument("target")
    v.add_argument("text")
    v.add_argument("--enter", action="store_true", help="press Enter after typing")

    v = verb("press", "press key(s) on the page. ex: ebrowse press Enter | Control+a")
    v.add_argument("keys")

    v = verb("check", "check a checkbox/radio")
    v.add_argument("target")
    v = verb("uncheck", "uncheck a checkbox")
    v.add_argument("target")

    v = verb("select", 'native <select> option by visible text. ex: ebrowse select @e7 "Canada"')
    v.add_argument("target")
    v.add_argument("value")

    v = verb("scroll", "scroll page or to a target. ex: ebrowse scroll down | ebrowse scroll s4")
    v.add_argument("direction", help="down | up | <sid> | @ref")
    v.add_argument("--pages", type=int, default=1, help="viewport-heights to scroll")

    v = verb("upload", "set files on a file input. ex: ebrowse upload @e9 ./cv.pdf")
    v.add_argument("target")
    v.add_argument("files", nargs="+")

    v = verb("eval", "run JavaScript in the page; prints result then diff")
    v.add_argument("js")

    # --- Compound verbs (several steps, one diff) ---
    v = verb(
        "fill-form",
        'fill many fields at once. ex: ebrowse fill-form s2 --data \'{"Email": "a@b.c", "I agree": true}\'',
    )
    v.add_argument("section", help="form section id from the outline")
    v.add_argument("--data", required=True, help='JSON object {"field label": value}')

    v = verb("query", 'filter a list/table section. ex: ebrowse query s4 --filter "Pending"')
    v.add_argument("section", help="list/table section id from the outline")
    v.add_argument("--filter", default=None, help="regex/substring over item text")
    v.add_argument("--cols", default=None, help="comma-separated column names (tables)")
    v.add_argument("--cursor", type=int, default=0)
    v.add_argument("--limit", type=int, default=None, help="max rows shown (default 20)")

    v = verb("search", 'find search box, type, submit. ex: ebrowse search "espresso"')
    v.add_argument("query")
    v.add_argument("--in", dest="target", default=None, help="@ref/CSS of the search box")
    v.add_argument("--pick", default=None, help="click the suggestion matching this text")
    v.add_argument("--no-submit", action="store_true", help="type only; don't press Enter")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.verb:
        parser.print_help()
        return 0

    from ebrowse.cli.client import run_command

    return run_command(args)


if __name__ == "__main__":
    sys.exit(main())
