"""Probe llm_review_scoped with captured real outputs + a fake bundled gate."""
import subprocess, sys, tempfile, os, textwrap, json
from pathlib import Path

SCOPED = str(__import__("pathlib").Path(__file__).parent / "llm_review_scoped.py")
PY = sys.executable

def fake_gate(stdout, rc):
    f = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False)
    f.write("import sys\n")
    f.write(f"sys.stdout.write({stdout!r})\n")
    f.write(f"sys.exit({rc})\n")
    f.close()
    return f.name

def run(stdout, rc):
    g = fake_gate(stdout, rc)
    src = Path(SCOPED).read_text().replace(
        '_BUNDLED = Path.home() / ".claude" / "skills" / "plan-execute" / "scripts" / "llm_review_gate.py"',
        f'_BUNDLED = Path({g!r})')
    t = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False); t.write(src); t.close()
    p = subprocess.run([PY, t.name, "--level", "medium"], capture_output=True, text=True)
    return p.returncode

HIGH = '```json\n[{"file":"a.py","line":1,"severity":"high","summary":"real bug"}]\n```'
MED  = '```json\n[{"file":"a.py","line":1,"severity":"medium","summary":"nit"},{"file":"b.py","line":2,"severity":"low","summary":"nit2"}]\n```'
BAD  = 'the reviewer rambled and emitted no array at all'

cases = [
    ("known POSITIVE: a high finding must BLOCK",            HIGH, 1, 1),
    ("known NEGATIVE: only medium/low must PASS",            MED,  1, 0),
    ("empty findings from bundled (rc0) must PASS",          "",   0, 0),
    ("INDETERMINATE from bundled must stay INDETERMINATE",   "",   2, 2),
    ("findings claimed but unparseable must be INDETERMINATE", BAD, 1, 2),
]
fail = 0
for name, out, rc, want in cases:
    got = run(out, rc)
    ok = got == want
    fail += (not ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}: exit {got} (want {want})")
sys.exit(1 if fail else 0)
