"""CapriQuant backend package root.

This file makes the 'app' directory a proper Python package,
which is required for reliable absolute imports like
`from app.utils.symbols import ...` and `from app.live_data import ...`
when running uvicorn main:app from the python-backend directory.
"""