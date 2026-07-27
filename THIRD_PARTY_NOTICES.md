# Third-party notices

Agent Console depends on third-party Python and JavaScript packages. Those
packages are not relicensed by this repository and remain subject to the
license recorded by their respective distributions.

The authoritative dependency sets are:

- `uv.lock` and `pyproject.toml` for Python packages;
- `frontend/package-lock.json` and `frontend/package.json` for JavaScript
  packages;
- pinned container image references in the Compose files.

Release maintainers must review the complete dependency inventory and retain
all required attribution files before distributing wheels, containers, or
bundled frontend assets. Development-only packages are not included in release
artifacts unless a release process explicitly copies or bundles them.
`scripts/check_dependency_licenses.py` is a CI release gate: it rejects missing
license metadata and dependencies under strong-copyleft or source-available
terms until maintainers complete an explicit project-level review.

Known development tooling includes components under permissive licenses and
data packages with attribution requirements, including MPL-2.0 and CC-BY-4.0
entries recorded in `frontend/package-lock.json`. Their upstream license files
must remain intact whenever those components or datasets are redistributed.
