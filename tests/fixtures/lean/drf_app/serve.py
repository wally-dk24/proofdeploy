"""Start the synthetic project: create the tables, then serve on $PORT (stdlib server)."""

import os
import sys
from wsgiref.simple_server import WSGIRequestHandler, make_server

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tinyapp.settings")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import django

django.setup()

from django.core.management import call_command  # noqa: E402
from django.core.wsgi import get_wsgi_application  # noqa: E402


class Quiet(WSGIRequestHandler):
    def log_message(self, format, *args):
        pass


call_command("migrate", run_syncdb=True, verbosity=0)
make_server(
    "127.0.0.1", int(os.environ["PORT"]), get_wsgi_application(), handler_class=Quiet
).serve_forever()
