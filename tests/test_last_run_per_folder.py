"""The last-run memory belongs to the folder of the script (one folder per area, a copy of both tools in each).
Synthetic data only."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import postprocess_remaining as ppr
import preprocess_classified as pc
from test_reuse_and_messages import post_case, small_raster

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ("preprocess_classified.py", "postprocess_remaining.py")


def make_folder(name):
    d = Path(tempfile.mkdtemp()) / name
    d.mkdir()
    for t in TOOLS:
        shutil.copy(ROOT / t, d / t)
    return d


def env_for(home, state=None):
    e = {k: v for k, v in os.environ.items() if k != "HARVEST_TOOLS_STATE_DIR"}
    e["HOME"] = str(home)
    e["USERPROFILE"] = str(home)
    if state:
        e["HARVEST_TOOLS_STATE_DIR"] = str(state)
    return e


def run_tool(folder, tool, args, home, stdin="", state=None):
    other = Path(tempfile.mkdtemp())          # a different working directory than the script folder
    return subprocess.run([sys.executable, str(folder / tool)] + [str(a) for a in args], cwd=other, input=stdin,
                          capture_output=True, text=True, env=env_for(home, state), timeout=600)


def pre_args(d, out):
    r, l1 = small_raster(d)
    return ["--raster", r, "--l1", l1, "--out", d / out, "--min-acres", 0, "--workers", 2, "--max-memory-gb", 1]


def post_args(d, out):
    f, l1 = post_case(d)
    return ["--final", f, "--l1", l1, "--out", d / out, "--workers", 2, "--max-memory-gb", 2, "--utm", "EPSG:32631"]


@pytest.fixture
def two_folders():
    home = Path(tempfile.mkdtemp())
    a, b = make_folder("area_A"), make_folder("area_B")
    return home, a, b


def test_each_folder_remembers_only_its_own_run(two_folders):
    home, a, b = two_folders
    for f in (a, b):
        (f / "data").mkdir()
        (f / "data" / "post").mkdir()
    for folder, out in ((a, "out_a.gpkg"), (b, "out_b.gpkg")):
        p = run_tool(folder, TOOLS[0], pre_args(folder / "data", out), home)
        assert p.returncode == 0, p.stdout + p.stderr
        p = run_tool(folder, TOOLS[1], post_args(folder / "data" / "post", "post_" + out), home)
        assert p.returncode == 0, p.stdout + p.stderr
    for folder in (a, b):
        hidden = folder / ".harvest_tools"
        assert (hidden / "last_run_preprocess.json").is_file() and (hidden / "last_run_postprocess.json").is_file()
    # nothing in the home folder except the shared L1 cache
    written = {p.relative_to(home).parts[0] for p in home.rglob("*") if p.is_file()}
    assert written <= {".preprocess_classified"}, written
    assert not (home / ".preprocess_classified" / "last_run.json").exists()
    assert not (home / ".postprocess_remaining").exists()
    shown = {}
    for folder, other in ((a, b), (b, a)):
        tag = "out_" + folder.name[-1].lower()
        p = run_tool(folder, TOOLS[0], ["--ask"], home, stdin="n\n" * 20)
        assert "A previous run was found" in p.stdout, p.stdout + p.stderr
        assert f"(remembered for this folder: {folder.resolve()})" in p.stdout
        assert str(folder / "data" / (tag + ".gpkg")) in p.stdout
        assert str(other) not in p.stdout
        p2 = run_tool(folder, TOOLS[1], ["--ask"], home, stdin="n\n" * 20)
        assert f"(remembered for this folder: {folder.resolve()})" in p2.stdout
        assert str(folder / "data" / "post" / ("post_" + tag + ".gpkg")) in p2.stdout and str(other) not in p2.stdout
        shown[folder.name] = p.stdout
    print(shown["area_A"][:700])
    print(shown["area_B"][:700])


def test_reuse_across_output_names_stays_inside_one_folder(two_folders):
    home, a, b = two_folders
    args = pre_args(a, "first.gpkg")
    assert run_tool(a, TOOLS[0], args, home).returncode == 0
    new = list(args)
    new[new.index(a / "first.gpkg")] = a / "second.gpkg"
    p = run_tool(a, TOOLS[0], new, home)
    assert f"Reusing saved work from {(a / 'first_work').resolve()}" in p.stdout, p.stdout
    # the other folder has its own memory: it must not pick up the work of A
    p = run_tool(b, TOOLS[0], pre_args(b, "b_first.gpkg"), home)
    assert p.returncode == 0 and "Reusing saved work" not in p.stdout, p.stdout
    assert not (b / ".harvest_tools" / "last_run_preprocess.json").read_text().count("area_A")


def test_state_dir_override(two_folders):
    home, a, _ = two_folders
    state = Path(tempfile.mkdtemp()) / "mine"
    p = run_tool(a, TOOLS[0], pre_args(a, "o.gpkg"), home, state=state)
    assert p.returncode == 0, p.stdout + p.stderr
    assert (state / "last_run_preprocess.json").is_file() and not (a / ".harvest_tools").exists()


def test_old_global_record_is_ignored(two_folders):
    home, a, _ = two_folders
    old = home / ".preprocess_classified"
    old.mkdir()
    (old / "last_run.json").write_text('{"raster": "x.tif", "l1": "y.gpkg", "out": "z.gpkg"}')
    p = run_tool(a, TOOLS[0], ["--ask"], home, stdin="\n" * 3)
    assert "A previous run was found" not in p.stdout
    assert (old / "last_run.json").read_text() == '{"raster": "x.tif", "l1": "y.gpkg", "out": "z.gpkg"}'


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), reason="needs a normal user")
def test_read_only_script_folder_falls_back_to_home(two_folders):
    home, a, _ = two_folders
    data = Path(tempfile.mkdtemp())
    args = pre_args(data, "o.gpkg")
    os.chmod(a, 0o555)
    try:
        p = run_tool(a, TOOLS[0], args, home)
        assert p.returncode == 0, p.stdout + p.stderr
        line = f"NOTE: cannot save the last-run record next to the script ({a.resolve()}); using " \
               f"{home / '.preprocess_classified' / 'last_run.json'} instead."
        assert p.stdout.count("NOTE: cannot save the last-run record") == 1
        assert line in p.stdout
        assert (home / ".preprocess_classified" / "last_run.json").is_file()
        assert not (a / ".harvest_tools").exists()
        p = run_tool(a, TOOLS[0], ["--ask"], home, stdin="n\n" * 20)
        assert "A previous run was found" in p.stdout     # the fallback record is read back
    finally:
        os.chmod(a, 0o755)


def test_location_is_a_function_of_the_script_file(monkeypatch, tmp_path):
    monkeypatch.delenv("HARVEST_TOOLS_STATE_DIR")
    assert pc.last_run_file() == ROOT.resolve() / ".harvest_tools" / "last_run_preprocess.json"
    assert ppr.last_run_file() == ROOT.resolve() / ".harvest_tools" / "last_run_postprocess.json"


@pytest.mark.parametrize("mod, kind, home_dir, rec", [
    (pc, "preprocess", ".preprocess_classified", {"raster": "r", "l1": "l", "out": "o"}),
    (ppr, "postprocess", ".postprocess_remaining", {"final": "f", "l1": "l", "out": "o"}),
])
def test_failed_write_next_to_the_script_falls_back_to_home(monkeypatch, tmp_path, capsys, mod, kind, home_dir, rec):
    """os.access says writable (as on a mapped network drive) but the write fails: really trying decides."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("HARVEST_TOOLS_STATE_DIR")
    monkeypatch.setattr(pc, "_NOTED", set())
    real = pc._write_json

    def fail_next_to_script(path, obj):
        if ".harvest_tools" in str(path):
            raise OSError("network drive refuses")
        return real(path, obj)

    monkeypatch.setattr(pc, "_write_json", fail_next_to_script)
    assert os.access(ROOT, os.W_OK)
    mod.save_last_run(rec)
    mod.save_last_run(rec)
    out = capsys.readouterr().out
    assert out.count("NOTE: cannot save the last-run record") == 1
    assert f"NOTE: cannot save the last-run record next to the script ({ROOT.resolve()}); using " \
           f"{home / home_dir / 'last_run.json'} instead." in out
    assert (home / home_dir / "last_run.json").is_file()
    assert mod.load_last_run()["out"] == "o"          # this folder gets its own memory back


def test_two_read_only_folders_never_see_each_others_fallback_record(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("HARVEST_TOOLS_STATE_DIR")
    monkeypatch.setattr(pc, "_NOTED", set())
    rec = {"raster": "r", "l1": "l", "out": "o"}
    real = pc._write_json
    monkeypatch.setattr(pc, "_write_json", lambda p, o: (_ for _ in ()).throw(OSError("ro")) if ".harvest_tools" in str(p)
                        else real(p, o))
    a, b = tmp_path / "A" / "tool.py", tmp_path / "B" / "tool.py"
    for f in (a, b):
        f.parent.mkdir()
    pc.save_record(a, "preprocess", ".preprocess_classified", dict(rec, out="mill_a"))
    assert pc.load_record(a, "preprocess", ".preprocess_classified")["out"] == "mill_a"
    assert pc.load_record(b, "preprocess", ".preprocess_classified") is None
    pc.save_record(b, "preprocess", ".preprocess_classified", dict(rec, out="mill_b"))    # overwrites the single file
    assert pc.load_record(b, "preprocess", ".preprocess_classified")["out"] == "mill_b"
    assert pc.load_record(a, "preprocess", ".preprocess_classified") is None              # nothing, not the wrong mill
    old = home / ".preprocess_classified" / "last_run.json"
    old.write_text(json.dumps(rec))                                                       # an old record: no script_dir
    assert pc.load_record(a, "preprocess", ".preprocess_classified") is None
