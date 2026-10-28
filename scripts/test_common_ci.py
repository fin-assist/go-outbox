"""Run shared bootstrap regression cases against this consumer's copied entrypoint."""
import importlib.util
from pathlib import Path
import sys
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    """Load reviewed test definitions without modifying the immutable source cache."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.object(sys, "dont_write_bytecode", True):
        spec.loader.exec_module(module)
    return module


def load_tests(loader, tests, pattern):
    """Keep one case catalogue while exercising the local trust boundary."""
    local = load_module("consumer_common_ci", ROOT / "scripts/common_ci.py")
    common = local.source_dir(ROOT, local.read_config(ROOT))
    suites = []
    for filename in ("test_bootstrap.py", "test_cache_integrity.py"):
        shared = load_module("shared_" + filename[:-3], common / "tests" / filename)
        # Rebinding is an explicit seam; a renamed catalogue must fail closed.
        if not isinstance(getattr(shared, "bootstrap", None), type(local)):
            raise AssertionError("Shared bootstrap catalogue no longer exposes its module seam")
        shared.bootstrap = local
        suites.append(loader.loadTestsFromModule(shared))
    # The contract separately requires this local copy to equal the pinned bootstrap bytes.
    contract = load_module("shared_consumer_contract", common / "consumer_contract.py")
    return loader.suiteClass([*suites, contract.suite(ROOT, common)])
