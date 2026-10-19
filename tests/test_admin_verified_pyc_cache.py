"""#402: CPython import caches must be source-equivalent before root use.

This is a non-root fixture for both stdlib-only validators. Only the
OS-level root ownership checks are emulated on disposable temp cache files;
source/bytecode/header comparisons are exercised without mocking compile().
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import py_compile
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
VALIDATORS = (
    ("bootstrap", "examples/admin/obsidian-admin-root-launch.py"),
    ("stage", "tools/prepare_admin_operator_authority.py"),
)


@pytest.fixture(params=VALIDATORS, ids=lambda v: v[0])
def verifier(request, tmp_path, monkeypatch):
    role, relative = request.param
    spec = importlib.util.spec_from_file_location(
        f"_operator_import_cache_{role}", ROOT / relative,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    package = tmp_path / "obsidian_automation"
    package.mkdir()
    (package / "__init__.py").write_text(
        '"""A harmless package used by the CLI import fixture."""\n',
        encoding="utf-8",
    )
    (package / "cli_module.py").write_text(
        "from __future__ import annotations\nANSWER = 42\n",
        encoding="utf-8",
    )
    command = (
        "import sys;sys.path.insert(0,sys.argv[1]);"
        "from obsidian_automation import cli_module;"
        "assert cli_module.ANSWER==42"
    )
    environment = dict(os.environ)
    environment.pop("PYTHONDONTWRITEBYTECODE", None)
    environment.pop("PYTHONPYCACHEPREFIX", None)
    subprocess.run(
        [sys.executable, "-c", command, str(tmp_path)],
        check=True, env=environment, capture_output=True, timeout=15,
    )
    cache_dir = package / "__pycache__"
    assert len(list(cache_dir.glob("*.pyc"))) == 2

    # Emulate root UID only for the temporary directory and files.
    # Test the production verifier's filename/header/content/extension gates.
    original_lstat = Path.lstat

    def fake_lstat(path, *args, **kwargs):
        actual = original_lstat(path, *args, **kwargs)
        if path == cache_dir or path.parent == cache_dir:
            return SimpleNamespace(
                st_mode=actual.st_mode, st_uid=0,
                st_nlink=actual.st_nlink, st_size=actual.st_size,
            )
        return actual

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    if role == "bootstrap":
        monkeypatch.setattr(
            module, "rooted_file", lambda path, limit: path.read_bytes(),
        )
        verify = module.reject_unverified_import_artifacts
    else:
        monkeypatch.setattr(
            module, "_pinned_root_regular",
            lambda path, *, max_bytes: path.read_bytes(),
        )
        verify = module._verify_import_inventory

    return package, cache_dir, verify


def test_real_cli_generated_timestamp_caches_are_source_equivalent(verifier):
    package, cache, verify = verifier
    assert verify(package) is None
    assert sorted(x.name for x in cache.iterdir()) == sorted(
        [
            f"__init__.{sys.implementation.cache_tag}.pyc",
            f"cli_module.{sys.implementation.cache_tag}.pyc",
        ]
    )


def test_hash_based_cache_from_verified_source_is_accepted(verifier):
    package, cache, verify = verifier
    py_compile.compile(
        str(package / "cli_module.py"),
        invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH,
        doraise=True,
    )
    assert verify(package) is None


def test_source_equivalent_optimized_bytecode_is_accepted(verifier):
    package, cache, verify = verifier
    py_compile.compile(
        str(package / "cli_module.py"), optimize=1, doraise=True,
    )
    assert verify(package) is None


@pytest.mark.parametrize("corruption", [
    "body", "magic", "invalid_flags", "stale_header", "missing_source",
    "unexpected_name", "symlink_to_valid", "direct_shadow_native",
])
def test_tampered_or_unreviewed_cache_fails_closed(verifier, corruption):
    package, cache, verify = verifier
    cached = cache / f"cli_module.{sys.implementation.cache_tag}.pyc"
    if corruption in ("body", "magic", "invalid_flags", "stale_header"):
        data = bytearray(cached.read_bytes())
        position = {
            "body": -1,
            "magic": 0,
            "invalid_flags": 4,
            "stale_header": 8,
        }[corruption]
        data[position] ^= 0x7f
        cached.write_bytes(data)
    elif corruption == "missing_source":
        (package / "cli_module.py").unlink()
    elif corruption == "unexpected_name":
        (cache / "rogue.bytecode.pyc").write_bytes(cached.read_bytes())
    elif corruption == "symlink_to_valid":
        (cache / "link.pyc").symlink_to(cached)
    elif corruption == "direct_shadow_native":
        (package / "cli_module.so").write_bytes(b"ROGUE")

    with pytest.raises(RuntimeError, match="unreviewed_"):
        verify(package)


def test_source_mutation_with_existing_cache_is_rejected(verifier):
    package, cache, verify = verifier
    (package / "cli_module.py").write_text(
        "from __future__ import annotations\nANSWER = 99\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="unreviewed_"):
        verify(package)


def test_compilation_is_not_execution(verifier, tmp_path):
    package, cache, verify = verifier
    marker = tmp_path / "marker"
    sample = package / "not_executed.py"
    sample.write_text(
        "from pathlib import Path\nPath(" + repr(str(marker)) +
        ").write_text('DANGEROUS_SIDE_EFFECT')\n",
        encoding="utf-8",
    )
    # py_compile compiles without importing the module.
    py_compile.compile(str(sample), doraise=True)
    assert verify(package) is None
    assert not marker.exists()


@pytest.mark.parametrize("extension", ["pyc", "pyo", "so", "pyd"])
def test_direct_compiled_shadow_remains_forbidden(verifier, extension):
    package, cache, verify = verifier
    (package / f"cli_module.{extension}").write_bytes(b"unreviewed")
    with pytest.raises(RuntimeError, match="unreviewed_"):
        verify(package)


def test_symlinked_cache_directory_is_rejected(verifier, tmp_path):
    package, cache, verify = verifier
    outside = tmp_path / "elsewhere"
    cache.rename(outside)
    cache.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="unreviewed_"):
        verify(package)



@pytest.mark.parametrize("marshal_version", [0, 1, 2, 3])
def test_equivalent_noncanonical_marshal_layout_is_accepted(
    verifier, marshal_version,
):
    """A valid .pyc may encode the same CodeType using different ref markers."""
    import marshal

    package, cache, verify = verifier
    cached = cache / f"cli_module.{sys.implementation.cache_tag}.pyc"
    original = cached.read_bytes()
    expected = marshal.loads(original[16:])
    alternate = marshal.dumps(expected, marshal_version)
    assert marshal.loads(alternate) == expected
    # Versions <=2 do not encode the reference-table layout used by v4.
    if marshal_version == 2:
        assert alternate != original[16:]
    cached.write_bytes(original[:16] + alternate)
    assert verify(package) is None


@pytest.mark.parametrize("metadata_change", [
    "filename", "first_line", "code", "constants", "line_table", "exception_table",
])
def test_tampered_code_object_metadata_is_rejected(
    verifier, metadata_change,
):
    """Even a correctly framed/cache-tagged marshal stream is not authority."""
    import marshal

    package, cache, verify = verifier
    cached = cache / f"cli_module.{sys.implementation.cache_tag}.pyc"
    original = cached.read_bytes()
    code = marshal.loads(original[16:])
    if metadata_change == "filename":
        untrusted = code.replace(co_filename="/tmp/unverified.py")
    elif metadata_change == "first_line":
        untrusted = code.replace(co_firstlineno=code.co_firstlineno+1)
    elif metadata_change == "code":
        # A malformed but parseable instruction sequence must not be imported.
        untrusted = code.replace(co_code=b"\x00\x00")
    elif metadata_change == "constants":
        untrusted = code.replace(co_consts=(None, 123456789))
    elif metadata_change == "line_table":
        untrusted = code.replace(co_linetable=b"")
    else:
        untrusted = code.replace(co_exceptiontable=b"\xff")
    cached.write_bytes(original[:16] + marshal.dumps(untrusted, 2))
    with pytest.raises(RuntimeError, match="unreviewed_"):
        verify(package)


def test_non_code_marshal_payload_is_rejected(verifier):
    import marshal

    package, cache, verify = verifier
    cached = cache / f"cli_module.{sys.implementation.cache_tag}.pyc"
    original = cached.read_bytes()
    cached.write_bytes(original[:16] + marshal.dumps({"raw": "not executable code"}, 2))
    with pytest.raises(RuntimeError, match="unreviewed_"):
        verify(package)


def test_unused_trailing_marshal_bytes_are_rejected(verifier):
    package, cache, verify = verifier
    cached = cache / f"cli_module.{sys.implementation.cache_tag}.pyc"
    cached.write_bytes(cached.read_bytes() + b"\x00TRAILING_UNVERIFIED")
    with pytest.raises(RuntimeError, match="unreviewed_"):
        verify(package)


def test_nested_compiled_code_and_exception_metadata_are_checked(verifier):
    import py_compile

    package, cache, verify = verifier
    f = package / "complex_module.py"
    f.write_text(
        "def outer(bound):\n"
        "    def inner(value):\n"
        "        try:\n"
        "            return {v * 2 for v in range(value) if v < bound}\n"
        "        except ValueError:\n"
        "            return set()\n"
        "    return inner(7)\n",
        encoding="utf-8",
    )
    py_compile.compile(str(f), doraise=True)
    assert verify(package) is None
