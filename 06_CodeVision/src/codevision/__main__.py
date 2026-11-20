"""Command-line entry point: python -m codevision [PATH] [--port N] [--no-browser]."""
from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from urllib.parse import quote

from codevision import server


def use_readable_output() -> None:
    """Make the Korean messages readable wherever stdout and stderr end up.

    A console shows what it can (never crash on one that cannot show Korean).
    A pipe or a file, such as the desktop app's log, gets UTF-8: Windows Python
    would otherwise write the ANSI code page (cp949), which reads as garbage.
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:  # pythonw or a windowed build: there is nowhere to write
            continue
        try:
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass  # not a regular text stream (an IDE or a test harness): leave it as it is


def main(argv: list[str] | None = None) -> int:
    use_readable_output()

    parser = argparse.ArgumentParser(prog="codevision", description="프로젝트 폴더 구조를 트리 도식으로 보여 줘요.")
    parser.add_argument("path", nargs="?", help="바로 열어 볼 폴더 (생략하면 화면에서 골라요)")
    parser.add_argument("--port", type=int, default=server.DEFAULT_PORT,
                        help=f"포트 번호 (기본 {server.DEFAULT_PORT}, 사용 중이면 빈 포트를 써요)")
    parser.add_argument("--no-browser", action="store_true", help="브라우저를 자동으로 열지 않아요")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("포트 번호는 0~65535 사이로 적어 주세요.")

    folder = None
    if args.path:
        folder = os.path.abspath(os.path.expanduser(args.path.strip().strip('"')))
        if not os.path.isdir(folder):
            print(f"폴더를 찾을 수 없어요: {args.path}", file=sys.stderr)
            return 2

    httpd = server.create_server(port=args.port)
    if args.port and httpd.port != args.port:
        print(f"{args.port}번 포트가 사용 중이라 {httpd.port}번으로 열었어요", flush=True)
    print(f"CodeVision이 열렸어요: {httpd.url}  (끄려면 Ctrl+C)", flush=True)
    target = httpd.url + "/"
    if folder:
        target += "?path=" + quote(folder, safe="")
    if not args.no_browser:
        webbrowser.open(target)
    elif folder:
        print(f"이 주소로 열면 바로 분석해요: {target}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("CodeVision을 껐어요.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
