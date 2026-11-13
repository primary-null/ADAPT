# Releasing ADAPT

ADAPT is a Python CLI, not an Android APK or desktop GUI installer. GitHub Releases carry a universal
wheel and a source distribution so users do not need Git or a compiler. Python 3.11+ and pipx are required.
The optional MCP extra adds its Python dependencies. Native receiver source is experimental and is not
included as a runnable desktop program in the Python package.

## Release procedure

1. Update the matching versions in `pyproject.toml` and `src/adapt_adb/__init__.py` for a new release.
2. Run offline tests, build with `python -m build`, check metadata with `python -m twine check dist/*`,
   and run `python scripts/check_distribution.py dist --tag vVERSION`.
3. Review and commit only intended public files. Tag `vVERSION` and push that tag to this repository.
4. `.github/workflows/release.yml` tests the source, builds the packages on GitHub, verifies package
   contents and checksums, and installs the built wheel on Windows/Linux with Python 3.11 and 3.13.
   It checks base CLI and optional MCP installation without contacting a device. The source archive
   is separately installed on Linux.
5. Only after every gate passes, the workflow attests all files and publishes the complete alpha
   pre-release. A manual workflow run tests packaging but does not publish anything. Never replace
   an asset in a published release; fix it in a new version.
6. Download the wheel from the public release without a GitHub login. Check SHA256SUMS.txt and test
   the exact README pipx commands from a clean temporary pipx home before claiming delivery.

Alpha releases are marked pre-release on GitHub. `/releases/latest` excludes pre-releases, so README
download links use a specific published tag and versioned asset URL. Update those links each release.
Do not point users to Actions artifacts: those require sign-in and expire.

## Verification

```text
gh attestation verify adapt_adb-VERSION-py3-none-any.whl --repo primary-null/ADAPT
```

Checksums detect an altered file relative to the published checksum list. GitHub's attestation additionally
ties the artifact to this repository's build workflow. Neither establishes Android device/runtime acceptance.
No PyPI account or publication is required for this distribution path. If PyPI is added later, use PyPA's
Trusted Publishing, reserve/verify the package identity first, and keep GitHub and registry versions aligned.

## Official guidance

- [PyPA packaging projects](https://packaging.python.org/en/latest/tutorials/packaging-projects/)
- [pipx installation and direct URL requirements](https://pipx.pypa.io/stable/how-to/install-pipx.html)
- [GitHub linking to releases](https://docs.github.com/en/repositories/releasing-projects-on-github/linking-to-releases)
- [GitHub artifact attestations](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations)
