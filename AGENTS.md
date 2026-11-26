# pkgs.stead.ing

Publishes Steading's signed packages to the `pkgs.stead.ing` R2 bucket. The bucket and its custom domain are Terraform in `steading-ai/infra`, which is private; nothing here names an account, host or credential.

## Rules

- The signing key exists in two places: the `publish` environment's `SIGNING_KEY` secret and an offline copy. Never print it, write it outside `$RUNNER_TEMP`, or reference the `publish` environment from a job other than the publishing one. `docs/signing-key.md` is the procedure and the reasoning.
- Nothing is signed that hasn't had its attestation verified against the component's release workflow at that tag.
- Published objects under `pool/` are immutable: an rpm is signed once, when it enters `edge`, and `stable` serves the same object.

## Checks

`hk check --all` runs every linter, as the pre-commit hook (`hk install`) and CI do; the tools come from `mise.toml`. `python3 -m unittest discover -s tools` runs the unit specs, and `test/e2e.sh` runs the publishing job end to end in a Fedora container. A workflow change needs `gh actions-lock` to update `.github/workflows/actions.lock`.
