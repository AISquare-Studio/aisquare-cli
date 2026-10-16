"""The nightly E2E's report: what one leg's run.json becomes for the people who read it.

- :mod:`e2e.report.build` turns ``<run>/run.json`` into ``<run>/report/``: a
  self-contained ``gallery.html``, a ``summary.md`` for an issue or a PR
  comment, a ``step-summary.md`` for the job page, and ``verdict.json`` in the
  release gate's shape.
- :mod:`e2e.report.publish` puts the screenshots where an issue can show them
  (an orphan branch of a repository the caller names) and posts the summary.
  Dry run unless told otherwise.
- :mod:`e2e.report.secret_scan` refuses a run holding a credential-shaped string,
  before anything of it is made public.

Standard library only, like :mod:`e2e.contracts`: both commands run in workflow
steps that have not installed the harness's requirements, and a report that
needs Jinja2 to say "the run failed" fails exactly when it is needed.
"""
