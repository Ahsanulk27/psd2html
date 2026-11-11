"""PSD to HTML or photocard: read a Photoshop design and have Claude rebuild it."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main() -> None:
    from .config import load_env

    load_env()
    for stream in (sys.stdout, sys.stderr):
        # Layer names and headlines are often Bengali; Windows consoles default to cp1252.
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(prog="psd2html", description=__doc__)
    commands = parser.add_subparsers(dest="command")

    serve = commands.add_parser("serve", help="run the web app and API (default)")
    serve.add_argument("--host", default="127.0.0.1")
    # Hosts such as Render say which port to listen on through PORT.
    serve.add_argument("--port", type=int, default=int(os.environ.get("PORT") or 8000))
    serve.add_argument("--reload", action="store_true", help="restart on code changes")

    convert = commands.add_parser("convert", help="convert one PSD to an HTML folder")
    convert.add_argument("psd", type=Path)
    convert.add_argument("-o", "--out", type=Path, help="output folder (default: <name>-html)")

    photocard = commands.add_parser("photocard", help="read one PSD as a Palette photocard layout")
    photocard.add_argument("psd", type=Path)
    photocard.add_argument("-o", "--out", type=Path, help="output folder (default: <name>-photocard)")

    inspect = commands.add_parser("extract", help="only read the PSD (no AI); for debugging")
    inspect.add_argument("psd", type=Path)
    inspect.add_argument("-o", "--out", type=Path, help="output folder (default: <name>-extract)")

    args = parser.parse_args()

    if args.command in (None, "serve"):
        import uvicorn

        uvicorn.run(
            "psd2html.server:app",
            host=getattr(args, "host", "127.0.0.1"),
            port=getattr(args, "port", int(os.environ.get("PORT") or 8000)),
            reload=getattr(args, "reload", False),
        )
        return

    if not args.psd.is_file():
        sys.exit(f"No such file: {args.psd}")

    if args.command == "extract":
        from .extract import extract

        out = args.out or args.psd.with_name(f"{args.psd.stem}-extract")
        result = extract(args.psd, out / "work", out / "output")
        print(f"Canvas {result.width}x{result.height}, {len(result.slices)} slice(s), "
              f"{len(result.assets)} asset(s), fonts: {', '.join(result.fonts) or 'none'}")
        for warning in result.warnings:
            print("warning:", warning)
        print(f"Wrote {out}")
        return

    from .generate import GenerationError

    def report(**changes):
        if "message" in changes:
            print(changes["message"] + "...", flush=True)
        if "chars_written" in changes:
            print(f"\r  {changes['chars_written']:,} characters written", end="", flush=True)

    if args.command == "photocard":
        from .jobs import convert_photocard

        out = args.out or args.psd.with_name(f"{args.psd.stem}-photocard")
        try:
            output = convert_photocard(args.psd, out, report, file_name=args.psd.name)
        except GenerationError as exc:
            sys.exit(f"\nError: {exc}")
        status = _read_json(output / "photocard.json")
        print(f"\nDone: {output / 'photocard.json'}")
        for warning in status.get("warnings", []):
            print("note:", warning)
        if status.get("missingFonts"):
            print("fonts to upload in Palette:", ", ".join(status["missingFonts"]))
        return

    from .jobs import convert as run_convert

    out = args.out or args.psd.with_name(f"{args.psd.stem}-html")
    try:
        output = run_convert(args.psd, out, report)
    except GenerationError as exc:
        sys.exit(f"\nError: {exc}")
    print(f"\nDone: {output / 'index.html'}")


def _read_json(path: Path) -> dict:
    import json

    return json.loads(path.read_text(encoding="utf-8"))
