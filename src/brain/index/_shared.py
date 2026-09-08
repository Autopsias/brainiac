"""Index dependency definitions."""
from __future__ import annotations

import concurrent.futures  # noqa: F401
import datetime as _dt  # noqa: F401
import hashlib as hashlib
import json as json
import math as math
import os as os
import re as re
import sqlite3 as sqlite3
import sys as sys
import time as time
from pathlib import Path as Path
from typing import Any as Any, Iterable as Iterable

from .. import classification as cls_mod  # noqa: F401
from .. import config as config, frontmatter as frontmatter
from ..chunk import chunk_text as chunk_text
from ..dbretry import with_write_retry as with_write_retry
from ..embed import Embedder as Embedder, EmbedderUnavailable as EmbedderUnavailable, get_embedder as get_embedder
from ..frontmatter import identifier_shaped as identifier_shaped, normalize_identity as normalize_identity, phrase_tokens as phrase_tokens
from ..injection_fold import UNKNOWN_VERDICT as UNKNOWN_VERDICT, stored_verdict as stored_verdict
from ..index_stages.search_identity import identity_owner_rowids as identity_owner_rowids, identity_records as identity_records, title_phrase_candidates as title_phrase_candidates
from ..notes import Note as Note, scan_vault as scan_vault
from ..progress import ProgressReporter as ProgressReporter
from ..vectors import SqliteVecBackend as SqliteVecBackend, VectorBackend as VectorBackend, get_backend as get_backend
from ._models import Hit as Hit, _ExactLeg as _ExactLeg, _FamilyCollapse as _FamilyCollapse, _NotePlan as _NotePlan, _ResumeState as _ResumeState, _SearchTrace as _SearchTrace
from ._settings import (
    EXACT_FULL_CAP as EXACT_FULL_CAP,
    EXACT_PARTIAL_CAP as EXACT_PARTIAL_CAP,
    EXACT_WEIGHT_COLLIDING_FULL as EXACT_WEIGHT_COLLIDING_FULL,
    GrepPatternError as GrepPatternError,
    EXACT_WEIGHT_PARTIAL_TITLE as EXACT_WEIGHT_PARTIAL_TITLE,
    EXACT_WEIGHT_UNIQUE_FULL as EXACT_WEIGHT_UNIQUE_FULL,
    FAMILY_MIN_BODY as FAMILY_MIN_BODY,
    GREP_REGEX_TIMEOUT_S as GREP_REGEX_TIMEOUT_S,
    INDEX_FORMAT_VERSION as INDEX_FORMAT_VERSION,
    MAX_GREP_PATTERN_LEN as MAX_GREP_PATTERN_LEN,
    RRF_K_EXACT as RRF_K_EXACT,
    RRF_K_FUSE as RRF_K_FUSE,
    SCHEMA_VERSION as SCHEMA_VERSION,
    _TEMPORAL_INTENT_RE as _TEMPORAL_INTENT_RE,
    _boilerplate_patterns as _boilerplate_patterns,
    _env_float as _env_float,
    _family_collapse_enabled as _family_collapse_enabled,
    _family_min_body as _family_min_body,
    _fusion_k_from_env as _fusion_k_from_env,
    _matches_boilerplate_pattern as _matches_boilerplate_pattern,
    _recency_factor as _recency_factor,
    _today as _today,
)

__all__ = [name for name in globals() if not name.startswith('__')]
