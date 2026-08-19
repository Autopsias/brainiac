"""Index dependency definitions."""
from __future__ import annotations

import concurrent.futures
import datetime as _dt
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Iterable

from .. import classification as cls_mod
from .. import config, frontmatter
from ..chunk import chunk_text
from ..dbretry import with_write_retry
from ..embed import Embedder, get_embedder
from ..frontmatter import identifier_shaped, normalize_identity, phrase_tokens
from ..index_stages.search_identity import identity_owner_rowids, identity_records, title_phrase_candidates
from ..notes import Note, scan_vault
from ..progress import ProgressReporter
from ..vectors import SqliteVecBackend, VectorBackend, get_backend
from ._models import Hit, _ExactLeg, _FamilyCollapse, _NotePlan, _ResumeState, _SearchTrace
from ._settings import (
    EXACT_FULL_CAP, EXACT_PARTIAL_CAP, EXACT_WEIGHT_COLLIDING_FULL,
    GrepPatternError,
    EXACT_WEIGHT_PARTIAL_TITLE, EXACT_WEIGHT_UNIQUE_FULL, FAMILY_MIN_BODY,
    GREP_REGEX_TIMEOUT_S, INDEX_FORMAT_VERSION, MAX_GREP_PATTERN_LEN,
    RRF_K_EXACT, RRF_K_FUSE, SCHEMA_VERSION, _TEMPORAL_INTENT_RE, _boilerplate_patterns,
    _env_float, _family_collapse_enabled, _family_min_body, _fusion_k_from_env,
    _matches_boilerplate_pattern, _recency_factor, _today,
)

__all__ = [name for name in globals() if not name.startswith('__')]
