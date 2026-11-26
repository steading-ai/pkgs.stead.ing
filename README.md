# pkgs.stead.ing

The package repository for every Steading component, served at [pkgs.stead.ing](https://pkgs.stead.ing). It holds the list of components, the workflows that publish and promote their packages, and the site.

Fedora is the first distribution it serves. Each component's release builds and attests its own rpms; this repository verifies those attestations, signs the rpms and the repository metadata with the [Steading signing key](docs/signing-key.md), and publishes them to two channels:

- `edge` gets every release as soon as it is published.
- `stable` moves to the newest `edge` build that has been in `edge` for at least 48 hours and hasn't been yanked.

A component's release starts the `publish` workflow itself once it has published, so a release reaches `edge` within minutes. It does so with a token from the `steading-pkgs-dispatch` GitHub App, which is installed on this repository alone with Actions write. `publish` also runs on a 15-minute schedule: that is what promotes a build to `stable`, and it picks up a release whose own start failed.

## Install

```bash
sudo dnf config-manager addrepo --from-repofile=https://pkgs.stead.ing/steading.repo
sudo dnf install skift
```

To follow `edge` instead:

```bash
sudo dnf config-manager setopt steading.enabled=0 steading-edge.enabled=1
```
