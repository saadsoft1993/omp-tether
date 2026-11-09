## What changed and why

<!-- One logical change per pull request. Link the issue if there is one. -->

## How you tested it

<!-- Commands you ran and the operating system. `make check-contrib` must pass. -->

## Checklist

- [ ] One logical change, with tests (a bug fix has a test that fails without the fix)
- [ ] `make check-contrib` passes (not `make check`: that includes the maintainer's publication gate)
- [ ] The plugin's `CHANGELOG.md` is updated, and its README or threat model if behaviour or the trust model changed
- [ ] A change to a wire or file protocol updates `docs/protocol/`, the schema and the conformance suite
- [ ] New files are listed in `tools/public-tree-check/allowlist.txt`
- [ ] Nothing is on by default, and nothing contacts a network unless the user configures it or asks for it
- [ ] No secrets, personal paths, host names or addresses in the change
