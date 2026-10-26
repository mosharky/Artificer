# Anvil plugin template

A starting point for an [Anvil](https://github.com/happy-dwarf-team/anvil) plugin for
Europa Universalis IV 1.37.5 (Steam, Windows x64), written in C++20 against plugin API 2.
It builds with the Anvil SDK's CMake package, checks the plugin in CI without the game
and releases it as a zip for `anvil/plugins/` and as a mod folder for the Workshop, which
it can also publish to the Steam Workshop.

The example plugin hooks a function (the game-speed object), hooks the middle of another
one (the return to the main menu) and has one setting.

The plugin uses plugin API 2 and needs [Anvil](https://github.com/happy-dwarf-team/anvil#readme)
1.0 or newer in the game.

## Your first plugin in 15 minutes

You need Windows x64, [Visual Studio 2022](https://visualstudio.microsoft.com/) or its
Build Tools with **Desktop development with C++** (it brings CMake 3.25 or newer), Git,
and EU4 1.37.5 with Anvil installed (`version.dll` next to `eu4.exe` and the `anvil/`
folder).

1. **Create your repository.** On GitHub, **Use this template → Create a new repository**,
   then clone it.

2. **Name your plugin.** Change the names below; the plugin id is lowercase letters,
   digits and `-`.

   | File | What to change |
   | --- | --- |
   | `plugin.toml` | `id`, `name`, `entry` (the DLL), `authors`, `description`, `issues`, `repository`, `affects_gameplay` |
   | `CMakeLists.txt` | `project(my_plugin …)` and the target in `anvil_add_plugin(my_plugin …)`; the `my_plugin-check` and `my_plugin-deploy` targets follow it |
   | `workshop/descriptor.mod` | `name`, the mod's name in the launcher and on the Workshop: `<Name> - <what it does>`, e.g. `Bellows - Faster Game Start` |
   | `workshop/description.md` | the Workshop description, from the skeleton, by the [Workshop page convention](https://github.com/happy-dwarf-team/.github/blob/main/docs/workshop-pages.md); replace `OWNER/REPO` |
   | `workshop/thumbnail.png` | the Workshop preview: a placeholder. Generate the real one with [tools/workshop-thumbnail](https://github.com/happy-dwarf-team/.github/tree/main/tools/workshop-thumbnail) from your plugin's icon on the Anvil website (add the icon to anvil-website first) |
   | `LICENSE` | the copyright holder, or another license |

3. **Declare your symbols.** Every game address the plugin touches is a
   `[symbol.<name>]` in `symbols.toml`: its `rva`, the `bytes` expected there, the
   `memory` kind and, for a function, its C `signature`. The two symbols in the template
   are real for the Steam 1.37.5 `eu4.exe`. `anvil gen` turns them into
   `symbols.gen.hpp` at build time (`sym::speed_object`), so a hook whose function does
   not match the signature does not compile. See
   [design §5](https://github.com/happy-dwarf-team/anvil/blob/main/docs/design.md) for
   every key.

4. **Build.** Point `EU4_EXE` at the game's `eu4.exe` and `EU4_DIR` at its folder:

   ```
   cmake -S . -B build -G "Visual Studio 17 2022" -A x64 -DEU4_EXE="C:/Program Files (x86)/Steam/steamapps/common/Europa Universalis IV/eu4.exe" -DEU4_DIR="C:/Program Files (x86)/Steam/steamapps/common/Europa Universalis IV"
   cmake --build build --config Release
   ```

   The first configure downloads the SDK archive of the Anvil release. The plugin lands in `build/plugin/my-plugin/`:
   `plugin.toml`, `symbols.toml`, `settings.toml` and `my_plugin.dll`.

5. **Check it against the game.**

   ```
   cmake --build build --config Release --target my_plugin-check
   ```

   `anvil check --exe` verifies every symbol against `eu4.exe` (build, bytes, memory,
   instruction boundaries) and rewrites `symbols.lock`. Commit `symbols.lock` with
   `symbols.toml`: CI has no game, so it fails when the lock does not match
   `symbols.toml` or says `result = "problems"`.

6. **Deploy.**

   ```
   cmake --build build --config Release --target my_plugin-deploy
   ```

   copies `build/plugin/my-plugin/` to `<EU4>/anvil/plugins/my-plugin/`. Close the game
   first: Windows does not let a loaded DLL be replaced.

7. **Run the game and read `anvil.log`.** Start EU4 as usual (Steam or the launcher)
   and open `<EU4>/anvil/anvil.log`. It lists your plugin with its status and its
   changes, then the plugin's own lines: `loaded on Anvil …`, the first game-speed
   object, and a line each time you return to the main menu. When a symbol does not
   verify, the log names the symbol and the byte, and the plugin is skipped while the
   game runs on.

Now replace `src/plugin.cpp` with your own plugin. `anvil.hpp` (the C++ wrapper) and
`anvil_plugin.h` (the C API underneath) document every host call.

## Settings

`settings.toml` declares what the player can change; `anvil gen` writes
`settings.gen.hpp`, so `cfg::log_menu_returns.get()` reads the current value. Anvil keeps
the player's values in `<EU4>/anvil/data/<id>/settings.toml`. Remove `settings` from
`plugin.toml` and the include from `src/plugin.cpp` when the plugin has none.

## Updating the SDK

The SDK version is the one place `CMakeLists.txt` names: the `FetchContent_Declare` of
`anvil_sdk`: a release URL with its `URL_HASH` from the release's `SHA256SUMS.txt`. A
newer release of the same major version keeps plugins working. After a change, delete `build/` (or run a fresh
configure) and rerun the `-check` target with `EU4_EXE` set.

## CI and releases

`.github/workflows/build.yml` runs on every pull request and push to `main`:

- builds the plugin with warnings as errors;
- runs `anvil check . --format github`: the schemas of the three TOML files and the
  `symbols.lock` check, as annotations on the files;
- packages both release zips (below) and keeps them as the run's artifact;
- copies the plugin into `workshop/anvil/<id>/` and runs a dry run of
  [paradox-workshop-publish](https://github.com/happy-dwarf-team/paradox-workshop-publish):
  the job summary shows what a release would publish to the Steam Workshop, and the job
  fails when the mod would change the game's checksum. It needs no secrets.

`pr-title.yml` checks that pull request titles follow
[Conventional Commits](https://www.conventionalcommits.org/) (see
[CONTRIBUTING.md](CONTRIBUTING.md)); they become the changelog. `release.yml` runs
[release-please](https://github.com/googleapis/release-please): it keeps a release pull
request with the next version (in `version.txt`, `plugin.toml` and
`workshop/descriptor.mod`) and `CHANGELOG.md`. Merging it tags `vX.Y.Z` and creates a
**draft** GitHub release with these assets; publish the draft on GitHub to make the
release public:

| Asset | For |
| --- | --- |
| `<id>-<version>.zip` | players: unpack into `<EU4>/anvil/plugins/` |
| `<id>-<version>-workshop.zip` | you: unpack into `Documents/Paradox Interactive/Europa Universalis IV/mod/` and upload the mod with the Paradox launcher |
| `SHA256SUMS.txt` | the hashes; the zips also get a build attestation |

The Workshop mod holds only `descriptor.mod` and `anvil/<id>/` (and the preview image
when [published from CI](#publish-to-the-steam-workshop)), which keeps the game's
checksum and ironman achievements. Anvil 1.3 and newer load plugins from enabled mods
after the player approves each version
([design §14](https://github.com/happy-dwarf-team/anvil/blob/main/docs/design.md));
players with an older Anvil install the plugin zip.

Repository settings that make this work (Settings on GitHub):

- **General → Pull Requests:** allow squash merging only, allow auto-merge,
  automatically delete head branches.
- **Rules → Rulesets:** a ruleset for the default branch that requires a pull request and
  the status checks `windows` and `conventional-title`, and blocks force pushes.
- **Actions → General → Workflow permissions:** allow GitHub Actions to create and approve
  pull requests (release-please opens its pull request with the workflow token).
- Optional secret `RELEASE_PLEASE_TOKEN` (a fine-grained token with contents and pull
  requests write): with it, CI also runs on the release pull request.

## Publish to the Steam Workshop

`.github/workflows/workshop-publish.yml` publishes the plugin as a Steam Workshop mod with
[paradox-workshop-publish](https://github.com/happy-dwarf-team/paradox-workshop-publish)
(`pdx-ws`) each time you publish a release. It is off until you turn it on: Anvil loads
plugins from Workshop mods from version 1.3 on, and not every plugin needs a Workshop item.

The mod is the `workshop/` folder (`workshop.toml`): `descriptor.mod`, `thumbnail.png`
and `anvil/<id>/`, the built plugin, which is not in Git. `checksum = "keep"` makes
`pdx-ws` refuse a mod that would change the game's checksum.

Once per plugin:

1. **Install `pdx-ws`** from its
   [releases](https://github.com/happy-dwarf-team/paradox-workshop-publish/releases)
   ([setup guide](https://github.com/happy-dwarf-team/paradox-workshop-publish/blob/main/docs/guide/setup.md)).

2. **Create the Workshop item** on your computer, with Steam running and logged in to the
   account that owns EU4 and will own the item. Build the plugin, copy it into the mod and
   check the plan first:

   ```
   cmake --build build --config Release
   New-Item -ItemType Directory workshop/anvil -Force
   Copy-Item build/plugin/my-plugin workshop/anvil -Recurse -Force
   pdx-ws plan
   pdx-ws init
   ```

   `pdx-ws init` (client mode, through the Steam client) creates a **private** item with
   the descriptor's name and tags and the preview, uploads the mod and writes the item's
   `remote_file_id` into `workshop/descriptor.mod`. Commit that line through a pull
   request; every later publish finds the item by it. Make the item public on Steam when
   you are ready: publishing never changes its visibility.

3. **Save a Steam session** in a `steam-workshop` environment. Read the
   [security notes](https://github.com/happy-dwarf-team/paradox-workshop-publish/blob/main/docs/guide/security.md)
   first; `session export` asks for your password and Steam Guard code once.

   ```
   gh api --method PUT repos/OWNER/REPO/environments/steam-workshop
   pdx-ws session export --user my_steam_name | gh secret set STEAM_SESSION --env steam-workshop
   gh secret set STEAM_USERNAME --env steam-workshop --body my_steam_name
   ```

   In **Settings → Environments → steam-workshop**, add yourself as a required reviewer
   and allow only the tags `v*`.

   Publishing several plugins from one GitHub organization? One session can serve them all
   as organization secrets limited to those repositories; the environment then keeps no
   secrets of its own but still gates every publish. Every workflow in those repositories
   can read an organization secret, so the
   [setup guide](https://github.com/happy-dwarf-team/paradox-workshop-publish/blob/main/docs/guide/setup.md#several-repositories-in-one-organization) explains when that is acceptable.

4. **Turn publishing on:** `gh variable set WORKSHOP_PUBLISH --body true`.

Then, for each release, merge the release pull request, wait until `release.yml` has
attached the zips to the draft release, edit its notes if you like and **publish the
draft**. `workshop-publish.yml` downloads the release's `<id>-<version>.zip`, checks its
build attestation (`gh attestation verify`, so exactly the build that `release.yml`
attested is published), unpacks it into `workshop/anvil/<id>/` and publishes the mod, with the release notes as the Workshop change note. The Workshop
description is left as you write it on Steam, unless you set `description` in
`workshop.toml`; then every publish replaces it. When the session expires, the job fails
with exit code 3: run `pdx-ws session export` again.

## Files

| Path | What |
| --- | --- |
| `plugin.toml` | the manifest: id, version, API, entry DLL, symbols, settings |
| `symbols.toml` | the game addresses the plugin uses, verified before the DLL loads |
| `symbols.lock` | the result of `anvil check --exe`; CI checks it is current |
| `settings.toml` | the player's settings |
| `src/plugin.cpp` | the plugin |
| `CMakeLists.txt` | fetches the Anvil SDK and calls `anvil_add_plugin` |
| `workshop/descriptor.mod` | the mod descriptor: its `version` is kept by release-please, and `pdx-ws init` adds `remote_file_id` |
| `workshop/thumbnail.png` | the Workshop preview |
| `workshop.toml` | how `pdx-ws` publishes `workshop/` to the Steam Workshop |
| `.github/` | CI, pull request titles, releases, Workshop publishing, Dependabot for the pinned actions |

## License

[MIT](LICENSE). Change it for your plugin as you like.

Anvil plugin template is an unofficial community project, not affiliated with or endorsed
by Paradox Interactive.
