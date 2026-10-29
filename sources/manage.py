#!/usr/bin/env python3
"""Management commands, e.g. ``python manage.py init-db --admin alice``.

Run ``python manage.py --help`` for the full list. Reads the same configuration
file as the web service (``../scholar.env`` or $SCHOLAR_CONFIG).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from flask.cli import FlaskGroup  # noqa: E402

from scholar import create_app  # noqa: E402

cli = FlaskGroup(create_app=lambda: create_app(), add_default_commands=False)

if __name__ == "__main__":
    cli()
