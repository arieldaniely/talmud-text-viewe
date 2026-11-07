"""Serve the viewer and its ignored local illustration data on loopback."""

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT/'.sefaria-cache/local-viewer'


class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        public = Path(super().translate_path(path))
        relative = public.relative_to(ROOT)
        if any(part.startswith('.') for part in relative.parts):
            return str(ROOT/'missing-local-resource')
        if relative.as_posix()=='catalog.json' or relative.parts[:1]==('data',):
            local = LOCAL/relative
            if local.is_file():
                return str(local)
        return str(public)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8766)
    args = parser.parse_args()
    handler = partial(Handler,directory=str(ROOT))
    with ThreadingHTTPServer(('127.0.0.1',args.port),handler) as server:
        server.serve_forever()
