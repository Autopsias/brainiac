"""Compatibility import for the tracked durable-write temp-name helper."""
from __future__ import annotations

from .lock import _atomic_temp_path

atomic_temp_path = _atomic_temp_path
