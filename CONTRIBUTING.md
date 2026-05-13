# Contributing

Thanks for thinking about contributing to `lenz-io`. This SDK is the public
Python client for the Lenz Claim Verification API for AI Product Teams.

## Reporting issues

[Open an issue](https://github.com/lenzhq/lenz-io-python/issues) and include:

- SDK version (`pip show lenz-io`)
- Python version (`python --version`)
- Minimal reproducer
- Expected vs. actual behavior
- The `X-Request-ID` from any error message (helps us trace the request)

## Setting up locally

```bash
git clone https://github.com/lenzhq/lenz-io-python
cd lenz-io-python
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
pytest
```

## Running tests

```bash
pytest                    # all unit tests (no network)
pytest -k webhooks        # subset
pytest -m smoke           # opt-in staging smoke (needs LENZ_E2E_KEY)
```

The unit suite is fully mocked. The smoke suite runs against `lenz.io` (or
a staging URL via `LENZ_BASE_URL`) and is opt-in via the `smoke` marker.

## Style

- `ruff` for lint + format
- `mypy --strict` for typing
- Single quotes? Double. (matches `ruff format` default)

```bash
ruff check . && ruff format --check . && mypy src/lenz_io
```

## Compatibility promise

We follow [SemVer](https://semver.org/). Breaking changes to public
attributes (anything in `lenz_io.__all__`) require a major version bump.
The `X-Lenz-API-Version` header is pinned per SDK release so old SDK
clients keep working against the API version they shipped against, even
after the server moves to v2.

## License

MIT. By contributing you agree your contribution will be licensed under
the project's MIT license.
