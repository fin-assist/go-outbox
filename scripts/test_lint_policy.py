"""The forbidigo and depguard rules in the shared .golangci.yml reject the core of process-wide state (FIN-322).

The pinned golangci-lint (version from the pinned shared source) runs with the repository's
.golangci.yml on a generated probe module: one line per forbidden identifier, which must
be reported, chains and an aliased import, blank imports rejected by depguard, and the
allowed forms (constructors, adapters, methods on injected values), which must not. All
probe dependencies are pinned. The probe runs for linux and windows. Static checks pin analyze-types, the exclusions and the
immutable source shared with ci.yml.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

try:
    import yaml
except ImportError as error:  # a missing parser must fail the checks, never skip them
    raise ImportError("PyYAML is required by scripts/test_lint_policy.py: "
                      "install python3-yaml or `pip install pyyaml`") from error


ROOT = Path(__file__).resolve().parents[1]
COMMON = Path(subprocess.check_output(
    [sys.executable, str(ROOT / "scripts/common_ci.py")], text=True, timeout=180).strip())
subprocess.run([sys.executable, "-B", str(COMMON / "render_lint.py"), "--root", str(ROOT)],
               check=True, timeout=60)
CONFIG = ROOT / ".finassist-ci/generated/.golangci.yml"
LINT_VERSION = json.loads((COMMON / "versions.json").read_text())["golangci-lint"]
GO_MOD = (ROOT / "go.mod").read_text()


def required_version(module, go_mod):
    """The version go_mod requires for module, or None."""
    match = re.search(r"^\s*(?:require\s+)?" + re.escape(module) + r" (v\S+)", go_mod, re.M)
    return match[1] if match else None


def semver_key(version):
    """A sort key for a Go module version (vMAJOR.MINOR.PATCH[-pre][+build]); a release sorts above its prereleases."""
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?", version)
    if match is None:
        raise ValueError(f"not a semantic version: {version}")
    pre = match[4]
    identifiers = ()
    if pre is not None:
        identifiers = tuple((0, int(part), "") if part.isdigit() else (1, 0, part) for part in pre.split("."))
    return (int(match[1]), int(match[2]), int(match[3]), pre is None, identifiers)


def expected_pin(module, hand, go_mod):
    """The version the probe requires for module and whether tidy must keep it exactly.

    A module in go.mod: max(go.mod, the hand pin if set), exactly. A module absent from go.mod: the hand pin,
    which tidy may raise (a newer go.mod dependency can need it) but never lower. A module of FROM_GO_MOD
    has no hand pin and must be in go.mod.
    """
    from_go_mod = required_version(module, go_mod)
    if hand is None:
        if from_go_mod is None:
            raise AssertionError(f"{module} is not in go.mod; give it a hand pin in HAND_PINS instead")
        return from_go_mod, True
    if from_go_mod is None:
        return hand, False
    return max(from_go_mod, hand, key=semver_key), True


def pin_violation(module, pinned, resolved, exact):
    """Why the version tidy resolved breaks the pin, or None."""
    if resolved is None:
        return f"tidy dropped the pinned {module}"
    if exact:
        return None if resolved == pinned else f"tidy moved the pinned {module} off {pinned} to {resolved}"
    if semver_key(resolved) < semver_key(pinned):
        return f"tidy lowered the pinned {module} below {pinned} to {resolved}"
    return None


# The probe is hermetic: every module it compiles is pinned, so an upstream release cannot break CI.
# One rule (expected_pin): a module in go.mod gets max(go.mod, its hand pin) and tidy must keep it exactly; a
# module absent from go.mod gets its hand pin, which tidy may raise but never lower; FROM_GO_MOD modules
# follow go.mod exactly. Hand pins exist only for modules the probe imports. Renovate updates go.mod but does
# not read HAND_PINS: they are raised by hand in a reviewed PR, or when the probe needs a newer API.
GO_VERSION = re.search(r"^go (\S+)$", GO_MOD, re.M)[1]
HAND_PINS = {
    "github.com/prometheus/client_golang": "v1.23.2",
    "golang.org/x/net": "v0.57.0",
    "github.com/golang/glog": "v1.2.5",
    "k8s.io/klog": "v1.0.0",
    "k8s.io/klog/v2": "v2.130.1",
    "go.uber.org/automaxprocs": "v1.6.0",
    "github.com/KimMachineGun/automemlimit": "v1.0.1",
}
FROM_GO_MOD = ["go.opentelemetry.io/otel", "go.opentelemetry.io/otel/metric", "go.opentelemetry.io/otel/trace"]
PINS = {module: expected_pin(module, HAND_PINS.get(module), GO_MOD) for module in [*HAND_PINS, *FROM_GO_MOD]}

PROMETHEUS = "github.com/prometheus/client_golang/prometheus"
# import path -> identifiers that must be reported; every one is referenced as a value.
FORBIDDEN = {
    "fmt": ["Print", "Printf", "Println"],
    "log/slog": ["Default", "SetDefault", "With", "SetLogLoggerLevel", "Debug", "DebugContext", "Info",
                 "InfoContext", "Warn", "WarnContext", "Error", "ErrorContext", "Log", "LogAttrs"],
    "log": ["Print", "Printf", "Println", "Fatal", "Fatalf", "Fatalln", "Panic", "Panicf", "Panicln",
            "Default", "Writer", "SetOutput", "SetFlags", "SetPrefix", "Flags", "Prefix", "Output"],
    PROMETHEUS: ["MustRegister", "Register", "DefaultRegisterer", "DefaultGatherer"],
    PROMETHEUS + "/promauto": ["NewCounter", "NewCounterVec", "NewCounterFunc", "NewGauge", "NewGaugeVec",
                               "NewGaugeFunc", "NewHistogram", "NewHistogramVec", "NewSummary",
                               "NewSummaryVec", "NewUntypedFunc"],
    PROMETHEUS + "/promhttp": ["Handler"],
    "net/http": ["DefaultClient", "Get", "Head", "Post", "Handle", "HandleFunc", "ListenAndServe",
                 "ListenAndServeTLS"],
    "go.opentelemetry.io/otel": ["GetTracerProvider", "SetTracerProvider", "GetMeterProvider",
                                 "SetMeterProvider", "GetTextMapPropagator", "SetTextMapPropagator",
                                 "SetErrorHandler", "GetErrorHandler", "SetLogger"],
    "os/signal": ["Notify", "NotifyContext", "Ignore", "Reset", "Stop"],
    "os": ["Getenv", "LookupEnv", "Environ", "ExpandEnv", "Setenv", "Unsetenv", "Clearenv", "Args", "Exit",
           "Stdout", "Stderr", "Stdin", "Chdir"],
    "flag": ["Arg", "Args", "Bool", "BoolFunc", "BoolVar", "CommandLine", "Duration", "DurationVar", "Float64",
             "Float64Var", "Func", "Int", "Int64", "Int64Var", "IntVar", "Lookup", "NArg", "NFlag", "Parse",
             "Parsed", "PrintDefaults", "Set", "String", "StringVar", "TextVar", "Uint", "Uint64", "Uint64Var",
             "UintVar", "Usage", "Var", "Visit", "VisitAll"],
    "runtime/debug": ["SetCrashOutput", "SetGCPercent", "SetMaxStack", "SetMaxThreads", "SetMemoryLimit",
                      "SetPanicOnFault", "SetTraceback"],
    "runtime": ["GOMAXPROCS"],
}

# Chains and an aliased import: each marked line must be reported by its first identifier.
CHAINS = """package probe

import (
	"context"
	stdlog "log"
	"log/slog"
	"net/http"

	"github.com/prometheus/client_golang/prometheus"
	"go.opentelemetry.io/otel"
)

// Chains reaches process-wide state through a chain or an alias.
func Chains(ctx context.Context, c prometheus.Collector, req *http.Request) {
	prometheus.DefaultRegisterer.MustRegister(c) // want
	stdlog.Default().Printf("x")                 // want
	slog.Default().InfoContext(ctx, "x")         // want
	slog.With("k", "v").InfoContext(ctx, "x")    // want
	_, _ = http.DefaultClient.Do(req)            // want
	_ = otel.GetTracerProvider().Tracer("x")     // want
	stdlog.Printf("x")                           // want
}
"""

# Imported even blank, these change process-wide state in init: depguard rejects each import line.
BLANK = """package probe

import (
	_ "expvar"                                // want
	_ "github.com/KimMachineGun/automemlimit" // want
	_ "github.com/golang/glog"                // want
	_ "go.uber.org/automaxprocs"              // want
	_ "golang.org/x/net/trace"                // want
	_ "k8s.io/klog"                           // want
	_ "k8s.io/klog/v2"                        // want
	_ "net/http/pprof"                        // want
)
"""


def marked(text):
    return {number for number, line in enumerate(text.splitlines(), start=1) if line.endswith("// want")}

ALLOWED = """package probe

import (
	"context"
	"flag"
	"fmt"
	"io"
	"log"
	"log/slog"
	"net/http"
	"os"
	"runtime"
	"runtime/debug"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/trace"
)

// Allowed uses injected values only; none of these lines may be reported.
func Allowed(ctx context.Context, l *slog.Logger, reg prometheus.Registerer, c prometheus.Collector,
	client *http.Client, w io.Writer, tp trace.TracerProvider, mp metric.MeterProvider) {
	_ = tp.Tracer("x")
	_ = mp.Meter("x")
	set := flag.NewFlagSet("library", flag.ContinueOnError)
	set.SetOutput(w)
	_ = set.String("name", "", "usage")
	_ = set.Parse(nil)
	_, _ = flag.UnquoteUsage(set.Lookup("name"))
	_, _ = debug.ReadBuildInfo()
	_ = debug.Stack()
	_ = runtime.NumCPU()
	_ = runtime.GOMAXPROCS(0) //nolint:forbidigo // read-only default of a pool size the service may override
	_, _ = os.Getwd()
	_, _ = os.ReadFile("x")
	_ = os.Expand("$A", func(string) string { return "" })
	l.InfoContext(ctx, "x")
	l.With("k", "v").InfoContext(ctx, "x")
	slog.New(slog.NewTextHandler(w, nil)).ErrorContext(ctx, "x", slog.String("k", "v"))
	std := log.New(w, "", 0)
	std.SetOutput(w)
	std.Printf("x")
	reg.MustRegister(c)
	r := prometheus.NewRegistry()
	r.MustRegister(c)
	_ = prometheus.NewCounter(prometheus.CounterOpts{Name: "x"})
	promauto.With(reg).NewCounter(prometheus.CounterOpts{Name: "y"})
	promauto.With(r).NewGaugeVec(prometheus.GaugeOpts{Name: "z"}, nil)
	h := promhttp.HandlerFor(r, promhttp.HandlerOpts{})
	_ = promhttp.InstrumentMetricHandler(reg, h)
	mux := http.NewServeMux()
	mux.Handle("/", h)
	server := &http.Server{Handler: mux}
	_ = server.Serve
	_, _ = client.Get("http://example.invalid")
	_ = fmt.Sprintf("x")
	_, _ = fmt.Fprintf(w, "x")
}
"""


def module_path(import_path):
    return import_path.rsplit("/", 1)[-1]


def forbidden_file(imports):
    lines = ["package probe", "", "import ("]
    lines += [f'\t"{path}"' for path in imports]
    lines += [")", "", "// Forbidden references every forbidden identifier on its own line.", "func Forbidden() {"]
    expected = set()
    for path, names in imports.items():
        for name in names:  # every name of the family, one per line
            lines.append(f"\t_ = {module_path(path)}.{name}")
            expected.add(len(lines))
    for builtin in ["print", "println"]:
        lines.append(f'\t{builtin}("x")')
        expected.add(len(lines))
    lines.append("}")
    return "\n".join(lines) + "\n", expected


def probe_imports():
    """Every import path of the probe: the FORBIDDEN keys and the import lines of the fixed files."""
    paths = set(FORBIDDEN)
    for text in [CHAINS, BLANK, ALLOWED]:
        paths |= set(re.findall(r'^\t(?:\w+ )?"([^"]+)"', text, re.M))
    return paths


def owning_module(import_path, modules):
    """The longest module of modules that contains import_path (k8s.io/klog/v2 is not k8s.io/klog), or None."""
    owners = [module for module in modules if import_path == module or import_path.startswith(module + "/")]
    return max(owners, key=len, default=None)


def lint(root, tool, goos):
    command = [str(tool), "run",
               "--allow-parallel-runners", "--enable-only", "forbidigo,depguard", "--max-issues-per-linter", "0",
               "--max-same-issues", "0", "--output.json.path", "stdout", "--show-stats=false", "./..."]
    result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=600,
                            env=dict(os.environ, GOTOOLCHAIN="auto", GOOS=goos, GOFLAGS="-mod=mod"))
    report = json.loads(result.stdout.strip().splitlines()[-1]) if result.stdout.strip() else None
    if report is None:
        raise AssertionError(f"golangci-lint produced no report:\n{result.stderr}")
    return report.get("Issues") or [], result


class ConfigTests(unittest.TestCase):
    def test_forbidigo_matches_types_and_excludes_only_tests_and_pgtest(self):
        config = yaml.safe_load(CONFIG.read_text())
        self.assertIs(config["linters"]["settings"]["forbidigo"]["analyze-types"], True)
        excluded = sorted(rule["path"] for rule in config["linters"]["exclusions"]["rules"]
                          if "forbidigo" in rule.get("linters", []))
        self.assertEqual(excluded, sorted([r"_test\.go", "internal/pgtest/"]))

    def test_probe_uses_the_linter_version_of_ci(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
        pin = json.loads((ROOT / ".finassist-ci.json").read_text())["revision"]
        calls = {key: job for key, job in workflow["jobs"].items() if "uses" in job}
        self.assertTrue(calls, "The consumer must actually invoke shared workflows")
        for key, job in calls.items():
            self.assertTrue(job["uses"].startswith("fin-assist/go-service-template/.github/workflows/"))
            self.assertTrue(job["uses"].endswith("@" + pin))
            self.assertEqual(job["with"]["revision"], pin)
            gate = workflow["jobs"][key.removesuffix("-check")]
            self.assertEqual(gate["needs"], key)
            self.assertIn("always()", gate["if"])
            self.assertEqual(gate["env"]["RESULT"], "${{ needs." + key + ".result }}")
            self.assertEqual(gate["steps"][0]["run"], 'test "$RESULT" = success')


class PinTests(unittest.TestCase):
    GO_MOD = "module x\n\nrequire a.example/m v1.2.0\n\nrequire (\n\tb.example/m v2.1.0 // indirect\n)\n"

    def test_semver_order(self):
        ordered = ["v0.9.0", "v0.10.0", "v1.0.0-rc.1", "v1.0.0-rc.2", "v1.0.0-rc.10", "v1.0.0", "v1.0.1", "v1.10.0",
                   "v2.0.0"]
        self.assertEqual(sorted(reversed(ordered), key=semver_key), ordered)
        self.assertEqual(semver_key("v1.2.3+incompatible"), semver_key("v1.2.3"))
        self.assertLess(semver_key("v0.0.0-20260101000000-abcdef123456"), semver_key("v0.0.0"))
        with self.assertRaises(ValueError):
            semver_key("1.2.3")

    def test_required_version_reads_blocks_and_single_lines(self):
        self.assertEqual(required_version("a.example/m", self.GO_MOD), "v1.2.0")
        self.assertEqual(required_version("b.example/m", self.GO_MOD), "v2.1.0")
        self.assertIsNone(required_version("b.example", self.GO_MOD))
        self.assertIsNone(required_version("c.example/m", self.GO_MOD))

    def test_go_mod_above_the_hand_pin_wins_exactly(self):
        self.assertEqual(expected_pin("a.example/m", "v1.1.0", self.GO_MOD), ("v1.2.0", True))
        self.assertEqual(expected_pin("a.example/m", "v1.2.0-rc.1", self.GO_MOD), ("v1.2.0", True))

    def test_hand_pin_above_go_mod_wins_exactly(self):
        self.assertEqual(expected_pin("a.example/m", "v1.10.0", self.GO_MOD), ("v1.10.0", True))

    def test_hand_pin_absent_from_go_mod_is_a_minimum(self):
        self.assertEqual(expected_pin("c.example/m", "v0.20.0", self.GO_MOD), ("v0.20.0", False))

    def test_from_go_mod_follows_go_mod_and_must_be_there(self):
        self.assertEqual(expected_pin("b.example/m", None, self.GO_MOD), ("v2.1.0", True))
        with self.assertRaises(AssertionError):
            expected_pin("c.example/m", None, self.GO_MOD)

    def test_exact_pins_stay_exact(self):
        self.assertIsNone(pin_violation("m", "v1.2.0", "v1.2.0", exact=True))
        self.assertIn("moved", pin_violation("m", "v1.2.0", "v1.3.0", exact=True))
        self.assertIn("moved", pin_violation("m", "v1.2.0", "v1.1.0", exact=True))

    def test_minimum_pins_may_only_rise(self):
        self.assertIsNone(pin_violation("m", "v0.20.0", "v0.20.0", exact=False))
        self.assertIsNone(pin_violation("m", "v0.9.0", "v0.10.0", exact=False))
        self.assertIn("lowered", pin_violation("m", "v0.20.0", "v0.19.9", exact=False))
        self.assertIn("lowered", pin_violation("m", "v1.0.0", "v1.0.0-rc.1", exact=False))

    def test_dropped_pin_is_a_violation(self):
        self.assertIn("dropped", pin_violation("m", "v1.0.0", None, exact=False))
        self.assertIn("dropped", pin_violation("m", "v1.0.0", None, exact=True))

    def test_from_go_mod_has_no_hand_pin(self):
        for module in FROM_GO_MOD:
            self.assertNotIn(module, HAND_PINS, module)

    def test_owning_module_is_the_longest_match(self):
        modules = ["k8s.io/klog", "k8s.io/klog/v2", "a.example/m"]
        self.assertEqual(owning_module("k8s.io/klog", modules), "k8s.io/klog")
        self.assertEqual(owning_module("k8s.io/klog/v2", modules), "k8s.io/klog/v2")
        self.assertEqual(owning_module("k8s.io/klog/v2/sub", modules), "k8s.io/klog/v2")
        self.assertEqual(owning_module("a.example/m/sub", modules), "a.example/m")
        self.assertIsNone(owning_module("a.example/mx", modules))
        self.assertIsNone(owning_module("fmt", modules))

    def test_pins_and_probe_imports_match(self):
        modules = [*HAND_PINS, *FROM_GO_MOD]
        imported = {owning_module(path, modules) for path in probe_imports()}
        for module in modules:
            self.assertIn(module, imported, f"{module} is pinned but the probe does not import it")
        # The reverse: every non-stdlib import (its first path element has a dot) belongs to a pinned module.
        unpinned = sorted(path for path in probe_imports()
                          if "." in path.split("/")[0] and owning_module(path, modules) is None)
        self.assertEqual(unpinned, [], "the probe imports modules without a pin")


class ProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        root = cls.root = Path(cls.directory.name)
        shutil.copyfile(CONFIG, root / ".golangci.yml")
        requires = "".join(f"\t{module} {version}\n" for module, (version, _) in sorted(PINS.items()))
        (root / "go.mod").write_text(f"module example.com/probe\n\ngo {GO_VERSION}\n\nrequire (\n{requires})\n")
        common, cls.expected = forbidden_file(FORBIDDEN)
        (root / "forbidden.go").write_text(common)
        (root / "allowed.go").write_text(ALLOWED)
        (root / "chains.go").write_text(CHAINS)
        (root / "blank.go").write_text(BLANK)
        # Exclusions: tests and exactly internal/pgtest are exempt; a similar path is not.
        reference = 'package {pkg}\n\nimport "os"\n\nfunc Env() string {{ return os.Getenv("X") }}\n'
        (root / "probe_test.go").write_text(reference.format(pkg="probe").replace("Env()", "testEnv()"))
        for folder in ["internal/pgtest", "internal/pgtestx"]:
            (root / folder).mkdir(parents=True)
            (root / folder / "env.go").write_text(reference.format(pkg=folder.rsplit("/", 1)[-1]))
        # Build the pinned linter once for the host; GOOS then selects only the analysed platform.
        install = subprocess.run(["go", "install", f"github.com/golangci/golangci-lint/v2/cmd/golangci-lint@{LINT_VERSION}"],
                                 capture_output=True, text=True, timeout=900,
                                 env=dict(os.environ, GOTOOLCHAIN="auto", GOBIN=str(root / "bin")))
        if install.returncode != 0:
            raise AssertionError(install.stderr)
        cls.tool = root / "bin" / "golangci-lint"
        tidy = subprocess.run(["go", "mod", "tidy"], cwd=root, capture_output=True, text=True, timeout=600,
                              env=dict(os.environ, GOTOOLCHAIN="auto"))
        if tidy.returncode != 0:
            raise AssertionError(tidy.stderr)
        resolved = (root / "go.mod").read_text()
        for module, (version, exact) in PINS.items():
            violation = pin_violation(module, version, required_version(module, resolved), exact)
            if violation:
                raise AssertionError(f"{violation}:\n{resolved}")

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def check(self, goos):
        issues, result = lint(self.root, self.tool, goos)
        others = [issue for issue in issues if issue["FromLinter"] not in ("forbidigo", "depguard")]
        self.assertEqual(others, [], result.stderr)
        reported = {(issue["Pos"]["Filename"], issue["Pos"]["Line"]) for issue in issues}
        expected = {("forbidden.go", line) for line in self.expected}
        expected.add(("internal/pgtestx/env.go", 5))
        expected |= {("chains.go", line) for line in marked(CHAINS)}
        expected |= {("blank.go", line) for line in marked(BLANK)}
        self.assertEqual(sorted(reported - expected), [], "reported outside the forbidden lines")
        self.assertEqual(sorted(expected - reported), [], "forbidden lines not reported")

    def test_linux(self):
        self.check("linux")

    def test_windows(self):
        self.check("windows")


if __name__ == "__main__":
    unittest.main()
