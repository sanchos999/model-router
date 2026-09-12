"""Control-plane package (spec R6 §B). Failure here must never break
inference: gateway/app.py wraps every control import in try/except."""
