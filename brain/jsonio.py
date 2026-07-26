"""One serialisation policy for the whole brain: nothing non-finite ever gets written.

`json.dumps` emits a bare `NaN` token, which is not valid JSON. Everything downstream of that is
a different failure with the same cause — the ledger on disk stops parsing, the dashboard row is
rejected, or (the one that already happened, on MU) the model reads "NaN" as if it were a number
and reasons over it. An indicator that could not be computed is None, and None is the honest
answer, so it is enforced at every write rather than at each call site.
"""
from __future__ import annotations

import json
import math
import sys


def json_safe(obj):
    """Recursively replace non-finite floats with None. Unwraps numpy scalars on the way."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    if hasattr(obj, "item") and not isinstance(obj, (str, bytes)):
        try:
            return json_safe(obj.item())
        except Exception:                      # pragma: no cover - exotic array types
            return str(obj)
    return obj


def dumps(obj, **kw):
    """`json.dumps` that refuses to emit NaN, and warns instead of failing the run."""
    kw.setdefault("default", str)
    try:
        return json.dumps(json_safe(obj), allow_nan=False, **kw)
    except ValueError as e:                    # pragma: no cover - a type json_safe cannot see
        print(f"[warn] payload still held a non-finite value ({e}); serialising loosely",
              file=sys.stderr)
        return json.dumps(obj, **kw)
