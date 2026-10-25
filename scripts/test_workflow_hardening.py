"""Workflow hardening that every library copies from the template (FIN-301).

Workflows are parsed with PyYAML (`yaml.safe_load`), so the checks see the decoded values
GitHub Actions evaluates: escapes, quoted keys, flow mappings and aliases are resolved by
the parser, not by hand-written patterns. The checks fail closed:
- every workflow file (`*.yml`, `*.yaml`) must parse, and there must be at least one;
- a checkout step outside release.yml and claude.yml sets `with.persist-credentials` to false;
- a `run` value contains no `${{` at all; values reach the script through `env:`.
"""
from pathlib import Path
import unittest

try:
    import yaml
except ImportError as error:  # a missing parser must fail CI, never skip the checks
    raise ImportError("PyYAML is required by scripts/test_workflow_hardening.py: "
                      "install python3-yaml or `pip install pyyaml`") from error


WORKFLOWS = Path(__file__).resolve().parents[1] / ".github/workflows"
# Release fetches main and publishes with the token; Claude's action manages its own git auth.
CREDENTIALED = {"release.yml", "claude.yml"}


def workflow_files():
    return sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])


def steps(document):
    """Every step of every job; a malformed shape is an error, not an empty list."""
    if not isinstance(document, dict) or not isinstance(document.get("jobs"), dict):
        raise ValueError("workflow has no jobs mapping")
    found = []
    for name, job in document["jobs"].items():
        if not isinstance(job, dict):
            raise ValueError(f"job {name!r} is not a mapping")
        job_steps = job.get("steps", [])
        if not isinstance(job_steps, list) or not all(isinstance(step, dict) for step in job_steps):
            raise ValueError(f"steps of job {name!r} are not a list of mappings")
        found.extend(job_steps)
    return found


def is_checkout(step):
    uses = step.get("uses")
    return isinstance(uses, str) and uses.strip().lower().startswith("actions/checkout@")


def keeps_token(step):
    """A checkout keeps the token unless its own `with.persist-credentials` is false."""
    inputs = step.get("with")
    value = inputs.get("persist-credentials") if isinstance(inputs, dict) else None
    return not (value is False or (isinstance(value, str) and value.strip().lower() == "false"))


def interpolates(step):
    run = step.get("run")
    if run is None:
        return False
    return not isinstance(run, str) or "${{" in run


def violations(text, credentialed=False):
    found = []
    for step in steps(yaml.safe_load(text)):
        if is_checkout(step) and not credentialed and keeps_token(step):
            found.append(("checkout", step))
        if interpolates(step):
            found.append(("run", step))
    return found


class WorkflowHardeningTests(unittest.TestCase):
    def test_workflows_are_found(self):
        names = [path.name for path in workflow_files()]
        self.assertIn("ci.yml", names)

    def test_workflows_are_hardened(self):
        for path in workflow_files():
            with self.subTest(workflow=path.name):
                self.assertEqual(violations(path.read_text(), path.name in CREDENTIALED), [])

    def test_checkouts_are_seen(self):
        # A walker that finds no steps would pass every workflow.
        text = (WORKFLOWS / "ci.yml").read_text()
        checkouts = [s for s in steps(yaml.safe_load(text)) if is_checkout(s)]
        self.assertEqual(len(checkouts), text.count("actions/checkout@"))


def job(*step_texts):
    return "on: push\njobs:\n  x:\n    runs-on: ubuntu-latest\n    steps:\n" + "".join(step_texts)


class DetectorTests(unittest.TestCase):
    """Every form reported in review must be caught; safe forms must pass."""

    def kinds(self, text):
        return [kind for kind, _ in violations(text)]

    def test_checkout_without_the_flag(self):
        self.assertEqual(self.kinds(job("      - uses: actions/checkout@v4\n        with:\n          fetch-depth: 0\n")),
                         ["checkout"])

    def test_checkout_with_the_flag(self):
        for value in ["false", "'false'", "False"]:
            with self.subTest(value=value):
                self.assertEqual(self.kinds(job(
                    f"      - uses: actions/checkout@v4\n        with:\n          persist-credentials: {value}\n")), [])

    def test_step_starting_with_another_key(self):
        for first in ["env:\n          A: b", "with:\n          fetch-depth: 0", "name: Checkout",
                      "id: co", "if: always()", "continue-on-error: true", "timeout-minutes: 1"]:
            with self.subTest(first=first):
                self.assertEqual(self.kinds(job(f"      - {first}\n        uses: actions/checkout@v4\n")), ["checkout"])

    def test_quoted_escaped_and_flow_uses(self):
        for step in ['      - uses: "actions/checkout@v4"\n', "      - uses: 'actions/checkout@v4'\n",
                     '      - uses: "actions/checkout\\x40v4"\n', '      - "uses": actions/checkout@v4\n',
                     "      - {name: c, uses: actions/checkout@v4}\n"]:
            with self.subTest(step=step):
                self.assertEqual(self.kinds(job(step)), ["checkout"])

    def test_flag_outside_the_checkout_inputs_does_not_count(self):
        for step in ["      - uses: actions/checkout@v4\n        env:\n          persist-credentials: false\n",
                     "      - uses: actions/checkout@v4\n        with:\n          ref: |\n            persist-credentials: false\n",
                     "      - uses: actions/checkout@v4\n        with:\n          persist-credentials: true\n"]:
            with self.subTest(step=step):
                self.assertEqual(self.kinds(job(step)), ["checkout"])

    def test_blank_lines_and_next_step(self):
        text = job("      - run: echo hi\n\n",
                   "      - uses: actions/checkout@v4\n\n",
                   "      - uses: actions/checkout@v4\n        with:\n          persist-credentials: false\n")
        self.assertEqual(self.kinds(text), ["checkout"])
        text = job("      - uses: actions/checkout@v4\n",
                   "      - uses: actions/setup-go@v5\n        with:\n          persist-credentials: false\n")
        self.assertEqual(self.kinds(text), ["checkout"])

    def test_every_expression_in_run_is_rejected(self):
        for expression in ["github.base_ref", "github.head_ref", "github.event.pull_request.title",
                           "github.event.commits[0].message", "github.event.workflow_run.head_branch",
                           "env.BASE_REF", "format('{0}', github.event.issue.title)",
                           "toJSON(github.event)", "inputs.tag", "steps.x.outputs.y"]:
            with self.subTest(expression=expression):
                self.assertEqual(self.kinds(job(f"      - run: echo ${{{{ {expression} }}}}\n")), ["run"])

    def test_block_scalars(self):
        for header in ["|", ">", "|-", ">-", "|+", ">+", "|2", "|-2", "| # comment", ">-  # x"]:
            with self.subTest(header=header):
                body = f"      - name: x\n        run: {header}\n          echo ok\n          echo ${{{{ env.A }}}}\n"
                self.assertEqual(self.kinds(job(body)), ["run"])

    def test_other_run_spellings(self):
        for step in ['      - "run": echo ${{ github.head_ref }}\n',
                     "      - run : echo ${{ github.head_ref }}\n",
                     "      - {name: x, run: 'echo ${{ github.head_ref }}'}\n",
                     '      - run: "echo $\\x7B{ github.head_ref }}"\n',
                     '      - run: "echo \\u0024{{ github.head_ref }}"\n',
                     "      - run: echo a\n          ${{ github.event.issue.title }}\n"]:
            with self.subTest(step=step):
                self.assertEqual(self.kinds(job(step)), ["run"])

    def test_alias_into_run(self):
        text = ("on: push\njobs:\n  x:\n    runs-on: ubuntu-latest\n    env:\n      S: &s echo ${{ github.head_ref }}\n"
                "    steps:\n      - run: *s\n")
        self.assertEqual(self.kinds(text), ["run"])

    def test_non_string_run_fails_closed(self):
        self.assertEqual(self.kinds(job("      - run: [echo, hi]\n")), ["run"])

    def test_expressions_outside_run_are_allowed(self):
        text = job("      - name: x\n        env:\n          BASE_REF: ${{ github.base_ref }}\n"
                   "        run: git diff \"origin/$BASE_REF...HEAD\"\n")
        self.assertEqual(self.kinds(text), [])

    def test_malformed_workflows_fail(self):
        for text in ["on: push\n", "jobs: []\n", "jobs:\n  x: 1\n", "jobs:\n  x:\n    steps: run\n",
                     "jobs:\n  x:\n    steps:\n      - echo\n"]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                violations(text)
        with self.assertRaises(yaml.YAMLError):
            violations("jobs:\n  x: [\n")

    def test_yaml_extension_is_collected(self):
        global WORKFLOWS
        original = WORKFLOWS
        try:
            import tempfile
            with tempfile.TemporaryDirectory() as directory:
                WORKFLOWS = Path(directory)
                (WORKFLOWS / "a.yaml").write_text(job("      - uses: actions/checkout@v4\n"))
                (WORKFLOWS / "b.yml").write_text(job("      - run: echo ok\n"))
                files = workflow_files()
                self.assertEqual([path.name for path in files], ["a.yaml", "b.yml"])
                self.assertEqual(self.kinds(files[0].read_text()), ["checkout"])
        finally:
            WORKFLOWS = original


if __name__ == "__main__":
    unittest.main()
