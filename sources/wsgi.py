"""WSGI entry point: ``gunicorn wsgi:app``."""
from scholar import create_app

app = create_app()
