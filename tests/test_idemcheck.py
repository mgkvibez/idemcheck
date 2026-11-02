"""The analyser and the CLI contract."""

import json

import pytest

from idemcheck.analyze import analyze_files, analyze_source
from idemcheck.cli import EXIT_FINDINGS, EXIT_OK, EXIT_USAGE, main

CHARGE = '''
@app.post("/charge")
def charge():
    db.session.add(order)
    db.session.commit()
'''


def test_a_post_that_writes_with_no_guard_is_high():
    routes, findings = analyze_source(CHARGE)
    assert routes == 1
    assert [f.severity for f in findings] == ["high"]
    assert findings[0].rule == "missing-idempotency"
    assert "db.session.commit" in findings[0].writes


def test_a_guard_anywhere_in_the_handler_clears_it():
    src = '''
@app.post("/charge")
def charge():
    key = request.headers.get("Idempotency-Key")
    db.session.commit()
'''
    assert analyze_source(src)[1] == []


def test_a_docstring_saying_todo_idempotency_is_the_bug_not_the_fix():
    """The first cut skipped the docstring node but not its string constant."""
    src = '''
@app.post("/charge")
def charge():
    """Charge the card. TODO: idempotency."""
    db.session.commit()
'''
    findings = analyze_source(src)[1]
    assert [f.severity for f in findings] == ["high"]


def test_put_and_patch_are_medium_because_they_should_already_be_idempotent():
    src = '''
@app.put("/order")
def update_order():
    db.session.commit()
'''
    findings = analyze_source(src)[1]
    assert [f.severity for f in findings] == ["medium"]


def test_a_read_only_route_is_not_a_finding():
    src = '''
@app.get("/orders")
def orders():
    return db.orders.find({})
'''
    routes, findings = analyze_source(src)
    assert routes == 0          # GET is not a mutating route
    assert findings == []


def test_a_mutating_route_that_only_reads_is_not_a_finding():
    src = '''
@app.post("/preview")
def preview():
    return db.orders.find_one({"id": 1})
'''
    routes, findings = analyze_source(src)
    assert routes == 1
    assert findings == []


def test_api_route_methods_keyword_is_understood():
    src = '''
@app.api_route("/charge", methods=["POST", "GET"])
def charge():
    db.session.commit()
'''
    findings = analyze_source(src)[1]
    assert len(findings) == 1 and findings[0].severity == "high"


def test_flask_route_methods_keyword_is_understood():
    src = '''
@app.route("/charge", methods=["POST"])
def charge():
    db.session.commit()
'''
    assert len(analyze_source(src)[1]) == 1


def test_flask_route_defaulting_to_get_is_not_a_finding():
    src = '''
@app.route("/health")
def health():
    db.session.commit()
'''
    assert analyze_source(src)[1] == []


def test_one_finding_per_handler_however_many_writes():
    src = '''
@app.post("/checkout")
def checkout():
    db.session.add(a)
    db.session.commit()
    payments.charge_card(a)
    queue.delay(a)
'''
    findings = analyze_source(src)[1]
    assert len(findings) == 1
    assert len(findings[0].writes) >= 3


def test_a_syntax_error_file_is_skipped_not_fatal():
    assert analyze_source("def broken(:\n") == (0, [])


def test_tests_are_excluded_by_default(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_api.py").write_text(CHARGE)
    (tmp_path / "app.py").write_text(CHARGE)

    assert analyze_files([tmp_path]).files == 1
    assert len(analyze_files([tmp_path]).findings) == 1
    assert analyze_files([tmp_path], include_tests=True).files == 2


# --- CLI contract -------------------------------------------------------------


def test_cli_exits_1_on_a_high_finding(tmp_path, capsys):
    (tmp_path / "app.py").write_text(CHARGE)
    assert main([str(tmp_path)]) == EXIT_FINDINGS
    assert "missing-idempotency" in capsys.readouterr().out


def test_cli_exits_0_when_nothing_is_unguarded(tmp_path, capsys):
    (tmp_path / "app.py").write_text('@app.get("/x")\ndef x():\n    return 1\n')
    assert main([str(tmp_path)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "No issues found by idemcheck v" in out


def test_fail_on_none_never_fails_the_run(tmp_path):
    (tmp_path / "app.py").write_text(CHARGE)
    assert main([str(tmp_path), "--fail-on", "none"]) == EXIT_OK


def test_a_medium_finding_does_not_fail_the_default_threshold(tmp_path):
    (tmp_path / "app.py").write_text('@app.put("/x")\ndef x():\n    db.session.commit()\n')
    assert main([str(tmp_path)]) == EXIT_OK
    assert main([str(tmp_path), "--fail-on", "medium"]) == EXIT_FINDINGS


def test_json_output_is_parseable(tmp_path, capsys):
    (tmp_path / "app.py").write_text(CHARGE)
    main([str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["findings"] == 1
    assert payload["findings"][0]["severity"] == "high"


def test_a_missing_path_is_a_usage_error(capsys):
    assert main(["/no/such/path/here"]) == EXIT_USAGE
    assert "no such path" in capsys.readouterr().err


def test_version_exits_cleanly(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.startswith("idemcheck ")
