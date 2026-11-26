# The signing key

Every rpm `pkgs.stead.ing` serves, and every repository's `repomd.xml`, is signed with one OpenPGP key. dnf checks both (`gpgcheck=1`, `repo_gpgcheck=1`), so this key is what a Steading user trusts when they install the repository.

## The key

- RSA-4096, one primary key able to sign, with no subkeys and no expiry.
- Its public half is [`site/RPM-GPG-KEY-steading`](../site/RPM-GPG-KEY-steading), served at `https://pkgs.stead.ing/RPM-GPG-KEY-steading` and installed by the `steading-release` rpm. The fingerprint is on the site's front page.

RSA-4096 because every rpm and librepo that might read the repository verifies it, including Enterprise Linux releases older than the Fedora this repository serves. No expiry because an expired key breaks every user's upgrades at once, and the only thing that would prevent it is a yearly renewal someone has to remember.

## Where the secret key lives

There are exactly two copies:

- **The `SIGNING_KEY` secret of this repository's `publish` environment.** The environment accepts deployments only from `main`, and one job references it: the publishing job, which signs only rpms whose attestation it has verified against the component's own release workflow. A component's release holds no key.
- **An offline copy in the maintainer's 1Password**, with the revocation certificate gpg wrote when it made the key. It is what a new environment secret would be restored from.

Neither copy carries a passphrase. Both stores are encrypted, and a passphrase the publishing job needs would have to sit beside the key in the same environment.

## How it was generated

[`bin/generate-signing-key`](../bin/generate-signing-key) did it once, on the maintainer's Mac, signed in to 1Password (`op`) and GitHub (`gh`). The script is the procedure; what it guards against:

- **The disk.** The keyring is made on a 32 MiB RAM disk (`hdiutil attach ram://`), remounted `nobrowse` with `.metadata_never_index` so Spotlight never reads it, and detached when the script exits, whether or not it succeeded. Before generating, the script checks that the keyring directory really is on that RAM disk. macOS encrypts swap, so a page of gpg's memory written out under pressure is not readable from the disk either.
- **Crash dumps.** The script sets `ulimit -c 0` before anything else.
- **The terminal, history and clipboard.** The secret key only ever travels through pipes: from `gpg --export-secret-keys` into `op document create -` and into `gh secret set`. The script prints the fingerprint and the public key, nothing else.
- **A silent overwrite.** It refuses to run if the environment already has a `SIGNING_KEY`, or if `site/RPM-GPG-KEY-steading` exists.
- **A copy that isn't one.** It reads the 1Password copy back into a scratch keyring on the RAM disk and checks that it holds this key's secret half, then confirms the key is what was asked for: RSA-4096, able to sign, no expiry, no subkeys, and a test signature that verifies.

Entropy comes from the operating system. gpg's libgcrypt draws from macOS's kernel random number generator, which is seeded from the hardware's entropy sources at boot and does not run short. Nothing extra was installed to feed it; tools that claim to add entropy don't make a seeded kernel generator any stronger.

The key was not generated on a server, nor anywhere an agent session runs as the same user: any process with the maintainer's uid could read a keyring while it exists.

## Using it in a workflow

The publishing job imports `SIGNING_KEY` into a keyring under `$RUNNER_TEMP`, which the runner discards with the job, checks the imported key's fingerprint against the environment's `SIGNING_KEY_FINGERPRINT` variable, and signs with `rpmsign` and `gpg --detach-sign`.

## Rotating it

Only if it is compromised. A new key is generated the same way under a new name, and shipped in a new `steading-release` rpm that is signed with the old key and carries both public keys for a release, then only the new one. Users take the new key with their next upgrade and do nothing themselves.

## Passed over

- **An expiring key, renewed every year.** Forgetting a renewal breaks every user's upgrades at once, and a yearly renewal is a ceremony to keep remembering.
- **A key held in a cloud key service.** No tooling turned up that signs rpms and `repomd.xml` with a key it cannot export.
- **COPR.** It builds the packages itself and signs them with a key the project does not hold, so it cannot serve the attested binaries a release carries.
- **An offline primary key with a signing subkey in CI.** dnf accepts anything the subkey signs until users take an update, which is the same remedy a compromised primary key needs, so the split costs a second key's handling and buys little here.
