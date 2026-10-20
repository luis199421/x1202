# Publishing updates (apt repository on GitHub Pages)

Pi 5 Energy Plan is installed as the Debian package `pi5-energyplan`. Once the
steps below are done, every Pi that installed it gets new versions **through the
Pi's own updater** (the update icon in the taskbar) and through `sudo apt upgrade`.
Nothing is installed by itself: the updater shows the update, and you click Install.

How it works:

```
push tag v2.0.1 ──► GitHub Actions (.github/workflows/release.yml)
                     ├─ checks the tag matches VERSION in pi5_energyplan.py
                     ├─ builds pi5-energyplan_2.0.1_all.deb   (build-deb.sh)
                     ├─ adds it to the apt repository, signs it (publish-apt-repo.sh)
                     ├─ publishes the repository to the gh-pages branch → GitHub Pages
                     └─ makes a GitHub release with the .deb
Pi ──► apt source /etc/apt/sources.list.d/pi5-energyplan.sources (added by the package)
       https://luis199421.github.io/x1202/apt, checked with the key in
       /usr/share/keyrings/pi5-energyplan-archive-keyring.asc
```

## One-time setup

1. **Make the signing key** on your own computer or the Pi (needs `gpg`):

   ```bash
   sh packaging/make-signing-key.sh
   ```

   It writes the public key to `packaging/apt-repo-key.asc` and the private key to a
   temporary file, and prints that file's name.

2. **Add the private key as a GitHub secret**: on GitHub, go to the repository →
   **Settings → Secrets and variables → Actions → New repository secret**. Use the
   name `APT_SIGNING_KEY` and paste the whole private key file as the value. Then
   delete the file. Never commit the private key.

3. **Commit the public key**:

   ```bash
   git add packaging/apt-repo-key.asc
   git commit -m "Add apt repository key"
   git push
   ```

4. **Publish the first version** (see below). This creates the `gh-pages` branch.

5. **Turn on GitHub Pages**: **Settings → Pages → Build and deployment →
   Source: Deploy from a branch → Branch: `gh-pages`, folder `/ (root)` → Save**.
   After a minute, `https://luis199421.github.io/x1202/apt/dists/stable/Release`
   opens in the browser.

6. **On each Pi, install once from a copy that has the key** (`git pull`, then
   `sudo sh install.sh`, or the click installer). From then on, updates come through
   the updater. Pis installed before step 3 don't have the update source yet; this
   one install adds it.

## Publishing a new version

1. Raise `VERSION` in `pi5_energyplan.py` (for example `2.0.0` → `2.0.1`) and commit.
2. Tag the commit with the same number and push the tag:

   ```bash
   git tag v2.0.1
   git push origin v2.0.1
   ```

3. Watch **Actions → Release** on GitHub. When it's green, the Pis' updater offers
   the new version the next time it checks (or right away with `sudo apt update`).

The workflow refuses a tag that doesn't match `VERSION`, and a secret that doesn't
match `packaging/apt-repo-key.asc`. Every published version stays in the repository.

## Building a package by hand

```bash
sh packaging/build-deb.sh            # → dist/pi5-energyplan_<VERSION>_all.deb
sudo apt install ./dist/pi5-energyplan_*_all.deb
```

`install.sh` does exactly this, plus the bootloader settings, recommended UPS
settings and the menu entry / panel icon.
