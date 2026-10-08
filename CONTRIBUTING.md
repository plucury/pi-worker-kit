# Contributing

Thanks for taking a look. This project is a small orchestration CLI plus a
skill definition, so contributions are usually a focused patch, a doc fix, or a
reproducible bug report.

## Scope and expectations

- One narrow outcome and one deterministic check per change. Match the style of
  the surrounding code; this project optimizes for readability over cleverness.
- Do not add a default provider, model, or profile. Routing is
  configuration, and configuration belongs to the user.
- Never commit secrets, private profiles files, logs, receipts, contracts, or
  real run histories. Record examples and docs use placeholders only.
- Bug reports need a version, platform, Python version, Pi version, the exact
  command, and the sanitized failure code from the receipt. Redact paths and ids
  you do not want public.
- Security issues follow [SECURITY.md](SECURITY.md), not the issue tracker.

## Development setup

The project has no build step and no runtime dependencies beyond Python and Pi
itself. Use an isolated virtual environment for development and tests:

```bash
# workdir: a scratch checkout of this repository
python3 -m venv .venv
. .venv/bin/activate
python3 --version   # 3.11 or newer
```

Pi's own login (`/login` inside Pi) or your existing shell environment provides
auth for local runs. Do not write keys into this repository, into a fixture, or
onto a command line, and do not add an auth field or login bridge to any config
this project reads.

Development dependencies stay local and separate from end-user installs:

```bash
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements-dev.txt
```

The runtime itself uses only the Python standard library; `requirements-dev.txt`
pins tooling only (pytest and ruff). Never add a provider SDK or a live-client
dependency to it.

The bundled gate also has a Node.js test harness (Node.js 20 or newer). Set it
up with the local dev tooling:

```bash
# workdir: the same checkout
npm ci --legacy-peer-deps --ignore-scripts --no-audit --no-fund
npm test
```

`npm ci` installs exactly what the committed `package-lock.json` pins. The
matching `--legacy-peer-deps` flag is intentional: it stops npm from trying to
resolve or install the host SDK peer `@earendil-works/pi-coding-agent: "*"`,
which this extension uses from the Pi host rather than installing a separate
copy for these synthetic tests.

`npm test` runs `node --test tests/test_gate.mjs` — synthetic, offline tests
only. `jiti` 2.7 is a development dependency of those tests; the host SDK
`@earendil-works/pi-coding-agent` stays a peer dependency at `*`, and no
runtime bundled copies of either are shipped. If jiti is already installed on
this machine, the developer module specifier `PI_WORKER_JITI_PATH` can point
the tests at it to stay offline.

## Tests

The test suite is self-contained and must never call a real model provider. Its
fixtures use a fake Pi runner; keep it that way, and never add a test that needs
a live provider or spends tokens.

```bash
# workdir: a scratch checkout of this repository
. .venv/bin/activate
python3 -m pip install -r requirements-dev.txt
python3 -m pytest tests -q
python3 -m ruff check scripts tests
```

Run the JS gate tests alongside them when the change touches the extension or
package metadata (Node.js 20 or newer, after the npm setup above):

```bash
# workdir: a scratch checkout of this repository
npm ci --legacy-peer-deps --ignore-scripts --no-audit --no-fund
npm test
```

Run the suite before opening a change. A new behavior needs a test that fails
without it; a docs-only change needs the syntax checks in the next section.

## Documentation checks

Every fenced `bash` block in the docs is copyable as written: quoted variables,
no `\` followed by a trailing comment, no unquoted `<placeholder>` redirections.
Check them from a scratch checkout:

```bash
# workdir: a scratch checkout of this repository
python3 - <<'PY'
import pathlib, re, subprocess, tempfile
bad = []
for md in sorted(pathlib.Path('.').glob('**/*.md')):
    if '.venv' in md.parts:
        continue
    text = md.read_text(encoding='utf-8')
    for block in re.findall(r'```bash\n(.*?)```', text, re.S):
        with tempfile.NamedTemporaryFile('w', suffix='.sh', delete=False) as fh:
            fh.write(block)
            name = fh.name
        if subprocess.run(['bash', '-n', name]).returncode:
            bad.append(str(md))
print('bash syntax failures:', sorted(set(bad)) or 'none')
PY
```

Fix reported files before submitting. The guide references live in
`references/`; keep relative links between markdown files working when you move
a section.

## Commit and pull request shape

1. One topic per change; a mechanical rename does not ride with a behavior fix.
2. Describe the observable change and the check that proves it. Do not claim
   performance numbers or benchmarks you did not run.
3. Note any documentation the change makes stale, in the same change.
