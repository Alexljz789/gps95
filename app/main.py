"""Entry point for the GPS95 web application."""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time

from .demo import DemoSource
from .web import AppContext, create_app


def main(argv=None):
    parser = argparse.ArgumentParser(description="GPS95 web application")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--demo", action="store_true", default=os.environ.get("GPS95_DEMO") == "1")
    args = parser.parse_args(argv)

    os.makedirs("/var/lib/gps95", exist_ok=True)
    ctx = AppContext()
    ctx.demo = bool(args.demo)
    demo = None
    if args.demo:
        demo = DemoSource(ctx.state)
        demo.start()
    else:
        ctx.start()

    app = create_app(ctx)

    def shutdown(_signum, _frame):
        try:
            ctx.source.stop()
        except Exception:
            pass
        if demo:
            demo.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(f"GPS95 listening on http://{args.host}:{args.port} demo={args.demo}", flush=True)
    app.run(host=args.host, port=args.port, threaded=True, use_reloader=False, debug=False)


if __name__ == "__main__":
    main()
