"""XLSX handler — openpyxl. One `## Sheet: <name>` section per sheet, rendered
as a Markdown table (headers retained). Formula collapse (HARDENED, per the
session brief): prefer the last-computed CACHED value (``data_only=True``); a
formula with no cached value (never opened in Excel) falls back to the raw
formula text tagged `(formula, uncomputed)` rather than crashing or silently
dropping the cell."""
from __future__ import annotations

from pathlib import Path

from .base import ExtractResult, Handler, density_gate
from .tables import rows_to_markdown

try:
    import openpyxl
    _HAS_OPENPYXL = True
except ImportError:  # pragma: no cover
    _HAS_OPENPYXL = False

MAX_XLSX_BYTES = 100 * 1024 * 1024
MAX_ROWS_PER_SHEET = 20_000  # cap runaway sheets rather than hang


class XlsxHandler(Handler):
    extensions = (".xlsx",)
    dependency_name = "openpyxl"

    @classmethod
    def available(cls) -> bool:
        return _HAS_OPENPYXL

    @classmethod
    def extract(cls, path: Path) -> ExtractResult:
        if not _HAS_OPENPYXL:
            return ExtractResult.quarantine("missing_dependency:openpyxl")
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size > MAX_XLSX_BYTES:
            return ExtractResult.quarantine("file_too_large")

        try:
            wb_values = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
        except Exception as exc:
            return ExtractResult.quarantine(
                "xlsx_extraction_error", warnings=[f"{type(exc).__name__}: {exc}"]
            )
        try:
            wb_formulas = openpyxl.load_workbook(str(path), data_only=False, read_only=True)
        except Exception:
            wb_formulas = None

        sections: list[str] = []
        warnings: list[str] = []
        try:
            for name in wb_values.sheetnames:
                ws_v = wb_values[name]
                ws_f = wb_formulas[name] if wb_formulas is not None else None
                rows: list[list[object]] = []
                # C9: ws_f.cell(row=, column=) is RANDOM ACCESS on a
                # read_only workbook — each call silently re-parses the
                # whole sheet from the start, and it fired for every single
                # empty cell (quadratic). Iterate both workbooks' rows in
                # lockstep instead (each row read exactly once), only
                # consulting the formula row when the value cell is empty.
                if ws_f is not None:
                    row_pairs = zip(ws_v.iter_rows(), ws_f.iter_rows())
                else:
                    row_pairs = ((row, None) for row in ws_v.iter_rows())
                for r_idx, (row, f_row) in enumerate(row_pairs, start=1):
                    if r_idx > MAX_ROWS_PER_SHEET:
                        warnings.append(f"sheet {name!r} truncated at {MAX_ROWS_PER_SHEET} rows")
                        break
                    out_row = []
                    for c_idx, cell in enumerate(row):
                        val = cell.value
                        if val is None and f_row is not None and c_idx < len(f_row):
                            f_val = f_row[c_idx].value
                            if isinstance(f_val, str) and f_val.startswith("="):
                                val = f"{f_val} (formula, uncomputed)"
                        out_row.append(val)
                    rows.append(out_row)
                sections.append(f"## Sheet: {name}\n\n" + rows_to_markdown(rows))
        except Exception as exc:
            return ExtractResult.quarantine(
                "xlsx_extraction_error", warnings=[f"{type(exc).__name__}: {exc}"]
            )
        finally:
            wb_values.close()
            if wb_formulas is not None:
                wb_formulas.close()

        body = "\n".join(sections)
        reason = density_gate(body)
        if reason:
            return ExtractResult.quarantine(reason)
        return ExtractResult(
            markdown=body, warnings=warnings, metadata={"sheet_count": len(sections)}
        )
