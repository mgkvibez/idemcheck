#!/usr/bin/env bash
# Release gate for idemcheck. Every check here has caught a real problem in one of our
# repos at some point, or it would not be here. Run it three times before a tag.
set -uo pipefail
cd "$(dirname "$0")/.."

fail() { echo "GATE FAILED: $*"; exit 1; }

echo "== check 1/6: editable install"
python3 -m pip install -q -e ".[dev]" || fail "install"

echo "== check 2/6: version consistency (pyproject vs module)"
pv=$(python3 - <<'PY'
import pathlib, re
try:
    import tomllib
    print(tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["version"])
except ModuleNotFoundError:                     # Python 3.9/3.10 have no tomllib
    text = pathlib.Path("pyproject.toml").read_text()
    print(re.search(r'(?m)^version\s*=\s*"([^"]+)"', text).group(1))
PY
)
mv=$(python3 -c "import idemcheck;print(idemcheck.__version__)")
[ "$pv" = "$mv" ] || { echo "pyproject says $pv, module says $mv"; fail "version drift"; }
echo "version $pv consistent"

echo "== check 3/6: test suite"
python3 -m pytest -q || fail "tests"

echo "== check 4/6: the CLI honours its documented exit codes"
tmp=$(mktemp -d)
printf '@app.post("/charge")\ndef charge():\n    db.session.commit()\n' > "$tmp/bad.py"
printf '@app.post("/charge")\ndef charge():\n    key = request.headers.get("Idempotency-Key")\n    db.session.commit()\n' > "$tmp/good.py"
python3 -m idemcheck.cli "$tmp/bad.py" > /dev/null 2>&1
[ $? -eq 1 ] || { echo "expected exit 1 on an unguarded POST"; fail "cli exit 1"; }
python3 -m idemcheck.cli "$tmp/good.py" > /dev/null 2>&1
[ $? -eq 0 ] || { echo "expected exit 0 on a guarded POST"; fail "cli exit 0"; }
python3 -m idemcheck.cli "$tmp/nope.py" > /dev/null 2>&1
[ $? -eq 2 ] || { echo "expected exit 2 on a missing path"; fail "cli exit 2"; }
python3 -c "import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)" "$tmp"
echo "exit codes 1 / 0 / 2 as documented"

echo "== check 5/6: README install name matches the distribution name"
pyname=$(grep -m1 -E '^name[[:space:]]*=' pyproject.toml | sed -E 's/.*"(.*)".*/\1/')
docname=$(grep -m1 -oE 'pipx install [A-Za-z0-9._-]+' README.md | awk '{print $3}')
echo "   pyproject=$pyname README=${docname:-<none>}"
[ "$pyname" = "$docname" ] || fail "README says install '${docname:-nothing}', package is '$pyname'"

echo "== check 6/6: the built wheel actually contains the package"
whl=$(mktemp -d)
python3 -m pip wheel . -w "$whl" --no-deps -q || fail "wheel build"
python3 - "$whl" <<'PY'
import glob, sys, zipfile
wheels = glob.glob(sys.argv[1] + "/*.whl")
if not wheels:
    sys.exit("no wheel was produced")
inside = [n for n in zipfile.ZipFile(wheels[0]).namelist()
          if n.startswith("idemcheck/") and n.endswith(".py")]
if len(inside) < 3:
    sys.exit("wheel has %d module file(s); the package is missing" % len(inside))
print("   wheel contains %d module file(s)" % len(inside))
PY
[ $? -eq 0 ] || fail "wheel is missing the package; an editable install hides this"

echo
echo "ALL CHECKS PASSED"
