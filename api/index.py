"""Vercel entrypoint for the MineBank Flask application.

Keeping the Vercel function entrypoint under api/ avoids relying on
automatic framework detection of the legacy root app.py module.
"""
from app import app

handler = app
