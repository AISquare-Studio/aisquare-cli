# Contributing to aisquare

Thanks for your interest in aisquare! This is an early-stage, open-source
project and contributions are welcome.

## Development setup

Requires Python 3.11+.

```sh
python3 -m venv .venv
source .venv/bin/activate
make install          # editable install + dev tools (ruff, mypy, pytest)
```

> **Do not install the explainability extra into this checkout.**
> `pip install 'aisquare-cli[explainability]'` puts a second `aisquare` package
> ahead of your editable one, and from then on every command — including the
> ones that would have explained it — dies with
> `No module named 'aisquare.cli'`. Install the extra in a separate,
> non-editable environment instead. If you already did: `pip uninstall aisquare`.

## Before you open a PR

Run the full check suite — this is exactly what CI runs:

```sh
make check            # lint + typecheck + tests
```

Individual targets:

| Task | Command |
| --- | --- |
| Format + autofix | `make fmt` |
| Lint | `make lint` |
| Type-check (mypy strict) | `make typecheck` |
| Tests | `make test` |

CI (`.github/workflows/ci.yml`) runs lint, format-check, mypy and pytest on
Python 3.11–3.13, plus a packaging job that builds the wheel and smoke-tests the
`aisquare` / `asq` console scripts. All jobs must pass before a PR can merge.

### Proving a test can fail

Many guards in this suite are written to catch something specific, so the useful
question is not "does it pass" but "can it fail". The usual method is to break
the thing on purpose, watch the test go red, and restore. **That method has a
trap, and it is silent.**

Python caches compiled bytecode — including pytest's rewritten test modules —
and decides the cache is fresh by comparing the source's **modification time in
whole seconds and its size in bytes**. A mutation that changes neither is
invisible: the old bytecode runs and the file on disk is not the code under
test. Measured on this repo:

```sh
# a test asserting "AAA" == "AAA", edited to "AAA" == "BBB" and re-run
# within the same second
1 passed          # the failing assertion never ran
rm -rf __pycache__
1 failed          # same file, correct result
```

Same-size edits are more common than they sound: swapping two names of equal
length, reordering a tuple, changing a digit, transposing two arguments. Both
directions hurt — a restore that does not take leaves the mutation live, and a
mutation that does not take reports a false green, which is the worse one
because you then report a defect that is not there.

Two habits, and they cover different moments:

- **Prevent it.** `find . -name __pycache__ -type d -prune -exec rm -rf {} +`
  between the mutation and the measurement.
- **Detect it.** Assert *which* test fails, not that something failed. A proof
  whose expected outcome is one bit cannot distinguish "the mutation is wrong"
  from "the mutation never ran"; naming the expected failure makes a stale run
  visible, including in a proof you already wrote up.

Mutations to Markdown, JSON or any non-imported file are structurally immune —
there is no bytecode to go stale.

## Implementing a feature (stub → service)

Most commands are still stubs that exit `70`. Each one becomes real by replacing
a single `stub(...)` call in a `services/` module — the CLI wiring and function
signatures already exist. The flow is:

1. **Implement the service** in `src/aisquare/services/<domain>.py`. Services
   return data; they never parse CLI arguments or print. Persisted state goes
   through the `ContextStore` in `src/aisquare/core/store.py`.
2. **Render it** in the matching `src/aisquare/cli/<group>.py` command: parse,
   call the service, print (honouring `--json` via `get_state().json_output`).
   Shared rendering helpers live in `cli/common.py`.
3. **Move the command off the stub skip-list** in `tests/test_stubs.py`
   (`IMPLEMENTED`) and add real tests for the new behaviour.

See the README's [Architecture](README.md#architecture) section for the full
layout and the thin-CLI / service / core split.

## Writing a guard that still guards

**Read this if you are about to write one — not as a checklist to audit against.**
There is no list of the guards in this repo, and the set is **not computable**: a
heuristic sweep found 14 files and missed five that are unmistakably rule-shaped,
including two fixed the same day; widening it found 20 and still missed five while
pulling in ordinary behaviour tests that happen to contain `assert not`. False
negatives and false positives from the same instrument at the same time. A
hand-maintained registry was considered and declined, because a hand-kept list of
"which guards are swept" rots exactly like the hand-checks it would replace.
Four guards here already had the pattern below before anyone named it, which is
the useful part: **the pattern is discoverable from the problem.**

This repo has about a dozen AST- and document-level guards. Over one long shift
every single one was found, by deliberate sabotage, to be passing while checking
less than it claimed — and the failures fell into four shapes. They are all the
same question asked at different points: **which thing is the assertion actually
looking at?** The answer is reliably "the thing that was easy to reach from where
I was standing".

- **Downstream of the failure.** An assertion on `result.exception` cannot fail
  when the code under test wraps everything in `except Exception` — the swallow
  eats the tripwire too. Use state the swallow cannot reach. Likewise, a test
  that measures elapsed time *after* a call cannot detect a call that never
  returns; that is a hung build, not a failure.
- **Narrower than the property.** Redaction was asserted on one field of a
  twelve-field record while the claim was about the bytes on disk. Assert the
  artefact the claim is about.
- **Upstream of the failure.** Every meta-check watched the *walk* — "functions
  were found", "the allow list names real symbols" — while the *rule* had stopped
  consuming it. One `continue` inside an offender loop makes a guard inspect
  nothing and report a clean tree. Extraction is not the fix: a predicate can be
  extracted *and* unit-tested and still be called by nobody. **The loop must be
  reachable by a control**, with a positive case per shape it claims to catch and
  a negative case of correct code it must not accuse — without the negative half,
  the cheapest way to pass is a rule that accuses everything.
- **Well-formed but never executed.** Conflict markers inside a docstring are
  valid Python: the module parses, the tests pass, `make check` reports success.
  So do a gutted body, an unconditional skip, and an empty `parametrize` set —
  each collects, counts toward the total, and runs nothing.
- **Emptiness as both goal and symptom.** In a guard whose whole value is that a
  class *stays* closed — "nothing raises", "the set of undecided call sites is
  empty", "the diff equals the record" — a rule that has gone blind produces the
  correct-looking answer for free, and there is nothing in the output to tell
  success from blindness. Such a guard needs something it must still **see**, not
  only something it must not find.

Half a control is the usual trap. Controlling the *scanner* — "the sweep found
files", "the walk found functions", "the scanner would catch a new seam" — proves
the scanner works and says nothing about whether the rule still consults it. Six
guards here were found blind that way, including the one written specifically
because conflict markers had nearly shipped: it reported every file clean while
matching nothing, restoring exactly the hole it existed to close, behind a green
suite. Assume the half you did not think of is a rule.

The observed pattern in who finds these is worth knowing: people control what they
have just built and not what they inherited from themselves a cycle earlier. Sweep
your own old guards, and expect the unowned ones to be nobody's habit.

Two mechanical rules earned the hard way:

1. **A sabotage needs a control as much as a measurement does.** Assert the
   mutation actually changed the file before running anything, and print
   "anchor missed — result meaningless" otherwise. A sabotage that did not apply
   is a green run that means nothing, and it is indistinguishable from a guard
   working. A sabotage aimed at the wrong line, or along an axis nothing uses,
   produces a real result for a fake reason. **Check the run as well as the
   mutation:** a mutation can apply and still break the file syntactically, or be
   pointed at a file that collects no tests, and then pytest emits no pass/fail
   line at all — "not caught" for a run that never happened reads exactly like a
   finding.
2. **Break the rule, not the assertion's input.** Replacing an input with a
   literal — `offenders = []` just above `assert not offenders` — is available in
   every test ever written, proves nothing about the guard, and simulates *the
   rule found nothing*, which is what a clean tree looks like. The defect is a
   rule the assertion still calls but which has stopped looking. **Both print
   green**, so a sweep without this distinction generates false instances as
   readily as real ones. Calling the rule *inside* the assert shrinks the surface;
   it does not close it, and nothing here can see an assertion whose input was
   replaced by hand.
3. **Anchor controls to synthetic inputs, not to production code.** A control
   pointed at a real function stops controlling anything the day that function is
   cleaned up — and controlling a falsifiability guard by gutting a real test
   means shipping the defect to demonstrate it.

On recorded numbers: a floor like `>= 900` is a constant anyone can lower while
doing something else, so prefer a property with no number in it — and before
inventing one, check whether the number is simply **redundant** (a broken walk
yields an empty set, which cannot be a superset of a non-empty allow list, so the
assertion beside it already fails). But a number that must stay true in **both
directions** is a control rather than a liability: the census guard survived the
sabotage that beat two others precisely because its recorded counts have an upper
bound as well as a lower one. The difference is the second direction.

## Conventions

- **Type everything.** `mypy` runs in `strict` mode over `src` and `tests`.
- **Tests are isolated.** The `isolated_home` fixture points `AISQUARE_HOME` at
  a temp dir, so tests never touch your real `~/.aisquare`. Never write to the
  real home in a test or example.
- Keep CLI modules thin and services free of CLI concerns.
- New shared plumbing goes in `core/`; new domain shapes go in `models.py`.

## Reporting bugs / proposing features

Open an issue describing what you expected and what happened. For larger
changes, it's worth opening an issue to discuss the approach before writing
code.
