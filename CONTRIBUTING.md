# Contributing

Every change goes to `main` through a pull request, which needs the `windows` (build)
and `conventional-title` checks to pass and is **squash-merged**: its title becomes the
commit on `main` and the changelog entry.

Titles follow [Conventional Commits](https://www.conventionalcommits.org/):
`<type>(<optional scope>): <summary in the imperative, lowercase, no period>`.

| Type | Use for | Release |
| --- | --- | --- |
| `feat` | something new for players | minor |
| `fix` | a bug fix | patch |
| `perf` | faster or lighter, same behaviour | patch |
| `revert` | reverting an earlier change | patch |
| `build` | CMake, the SDK version, packaging | patch |
| `docs`, `refactor`, `test`, `ci`, `chore` | everything else | none |

Before 1.0.0 a breaking change (`feat!:` with a `BREAKING CHANGE:` paragraph) bumps the
minor version.

When you change `symbols.toml`, run the `-check` target with `EU4_EXE` set and commit
the new `symbols.lock` in the same pull request; CI fails on a stale lock.
