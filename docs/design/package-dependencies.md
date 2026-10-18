# Package dependencies — a package that needs another package

- **Status:** Implemented in v1 scope (2026-10-18)
- **Authors:** Kjeld Oostra, Claude
- **Related code:** `backend/app/services/package_dependencies.py` (new),
  `backend/app/services/package_service.py`, `backend/app/services/package_repositories.py`,
  `backend/app/services/manifest_validator.py`, `backend/app/schemas/config.py`,
  `backend/app/api/v1/endpoints/packages.py`, `console/src/components/packages/*`
- **Related docs:** ADR 2026-10-13 package repositories, `docs-mint/admin/packages.mdx`,
  `docs-mint/admin/manifests.mdx`, the `sinas-platform/packages` repository README

## 1. Problem

A package can only be installed on its own. A package that builds on another
one (a "support-mailbox-triage" package whose agent uses the `gmail`
package's connector) has no way to say so: the operator has to know to install
`gmail` first. If they don't, the install fails on a missing reference (an
agent pointing at `gmail/api`), or worse, succeeds and breaks later when
`gmail` is uninstalled underneath it, because uninstall checks nothing.

What exists today:

- **References across packages already work by existence.** An agent's
  `enabledConnectors`, `enabledFunctions`, … are checked on apply
  (`AgentApplier.check_new_references`); a reference counts if a row of that
  `namespace/name` exists, whoever owns it. So a package can use another
  package's resources if that one happens to be installed first. Nothing
  records the relationship.
- **Uninstall deletes without looking.** `PackageService.uninstall` deletes
  every row stamped `pkg:<name>`; another package's agent referring to one of
  them is left dangling. (An *upgrade* that drops a resource is protected:
  `_prune_missing` refuses to remove anything a remaining row still uses.)
- **Manifests** (`spec.manifests`) declare `requiredResources`,
  `requiredPermissions` and `storeDependencies`, and `GET
  /api/runtime/manifests/{ns}/{name}/status` reports what is ready or missing.
  The config parser also refuses a manifest whose required resources exist
  nowhere, but only for four resource types, and it can't see what another
  package would provide.
- **The `dependencies` resource kind is unrelated** (it approves Python
  packages for function containers). Nothing here uses that name.

## 2. Manifests or package-level `requires`?

The earlier plan was "manifests as requires + status, then requires with
topological install order". Could manifests alone carry dependencies — the
package declares a manifest, install checks its status, and the repository
index maps resources to the packages providing them?

Not as the primary mechanism, for three reasons:

1. **Install needs a package, not a resource.** To install what's missing,
   Sinas has to know *which package* to fetch. A resource list would need
   every index entry to list every resource it provides, and a resource can
   be provided by more than one package (two connectors named `gmail/api` in
   two repositories), so the mapping is ambiguous exactly when it matters.
2. **Versions.** "gmail 1.x, but not 2.0, which renamed its connector" is a
   statement about a package version. A resource list can't express it, and
   upgrades/uninstall protection are about packages anyway.
3. **Uninstall and upgrade act on packages.** "Who depends on gmail" must be
   answerable from installed packages, without re-deriving it from resource
   lists of every installed manifest.

So: **package-level `requires` (name + version range) is what install,
preview, upgrade and uninstall act on.** Manifests keep their role and are
reused, not duplicated, for *verification* of what a package actually uses:

- It turns out the config parser already checks a manifest's
  `requiredResources` when a config or package is applied: one that exists
  neither in the same YAML nor in the database is a validation error, so the
  install is refused. That is exactly "verify what the dependent uses exists
  after resolution", as long as it can see what the dependencies provide.
  It now does: in a preview, the parser counts what the planned dependencies
  declare (`parse_and_validate(..., extra_names=...)`, the same "about to
  exist" set the appliers get, §7); in the install, the dependencies are
  already applied in the same transaction, so the database has them.
- The parser accepted only `agent`, `function`, `skill` and `collection`
  there, while a dependency is typically a connector. Both checks of
  `requiredResources` — the parser's and `/status`'s — now use one type
  table (`manifest_validator.MANIFEST_RESOURCE_TYPES`), extended with
  connectors, queries, stores, components, pipelines and MCP servers.
  (`/status` reported those types as missing even when they existed.)
- No second requirement schema: `requiredResources` is the resource-level
  statement, `package.requires` the package-level one.
- Deriving `requires` from manifest resources through the index (the index
  mapping resources to providing packages) is a possible follow-up, not v1.

## 3. Declaring dependencies

```yaml
package:
  name: support-mailbox-triage
  version: 1.0.0
  requires:
    - name: gmail
      version: ">=1.2,<2"           # optional: any version when left out
      repository: sinas-official    # optional: where to look first
      instance: gmail-support       # optional: which install, for multiInstance packages
```

Validated in `PackageMetadataConfig` (`PackageRequirement`):

- `name`: a package name (`^[a-z0-9][a-z0-9._-]*$`, the index's rule).
  Unique within the list; a package can't require itself.
- `version`: a comma-separated list of clauses, pyproject style, parsed by
  our own small parser (no new dependency): `>=`, `>`, `<=`, `<`, `==`, `!=`,
  `~=` and `==1.2.*`. Versions compare with the same semver-style key the
  update check already uses (`package_repositories._version_key`). A
  pre-release (`2.0.0-rc.1`) only matches a range in which a clause names a
  pre-release, as in PEP 440, so `<2` does not pull in `2.0.0-rc.1`. An
  unparseable range is a validation error; an installed version that isn't
  semver-style never satisfies a range (it does satisfy "any version").
- `repository`: a repository name hint (§5).
- `instance`: the install name to use (§4).

Since install-time variables are substituted into the whole YAML before it
is parsed, `instance: ${{ vars.GMAIL_INSTANCE }}` works and lets the operator
pick the instance at install (§4).

## 4. Multi-instance dependencies

The requirement targets an **install name**: `instance` if given, otherwise
the required package's own name — the name its default install has. So:

- `gmail` (single instance): `instance` is left out; the install named
  `gmail` must be package `gmail`.
- A multiInstance package: the dependent names the instance, statically or
  through a variable. Left out, it means the default instance (installed
  under the package's own name).

An install under that name that is a *different* package (an instance of
something else that happens to have that name) is "incompatible". When the
dependency is missing and gets installed, it is installed under that install
name — which, for a name other than the package's own, requires the package to
declare `multiInstance` (the existing `resolve_install_name` rule).

## 5. Resolving a plan

`package_dependencies.resolve_plan` walks the requirements depth-first and
returns an ordered plan, dependencies before dependents, with one entry per
required install:

| status          | meaning | install |
|-----------------|---------|---------|
| `satisfied`     | installed, the right package, version in range | nothing to do |
| `install`       | not installed; an active repository offers a version in range | installed first |
| `missing`       | not installed and no active repository offers a version in range | refused |
| `incompatible`  | installed, but out of range or a different package under that name | refused |
| `cycle`         | the requirement leads back to a package on its own path | refused |

Rules:

- **A satisfied dependency is not descended into.** Its own requirements were
  checked when it was installed.
- **A package to install is fetched (SHA-256 verified, as every repository
  install is) and its own `requires` resolved the same way**, recursively.
- **Diamonds** (two packages needing the same one) resolve to one entry; every
  constraint on it must hold, and its `required_by` lists both.
- **Cycles** fail with the path (`a → b → a`), including a requirement back to
  the package being installed.
- **Where a missing package comes from:** the active repositories offering a
  package of that name at a version in range, in this order: the
  requirement's `repository` hint, then the repository the dependent itself
  comes from, then the other active repositories by name. A repository's
  index lists one version per package (its latest), so "available" means
  "that version is in range". The hint is a preference, not a pin: every
  active repository is one the operator added. The plan names the repository
  each package comes from, so the operator sees it before confirming.
- **No automatic upgrades.** An installed dependency outside the range is
  `incompatible`; the operator updates it first (which runs the upgrade
  checks in §7). This keeps a plan to "install A" from silently changing an
  installed B under its other dependents.

The resolver has a recursion bound (depth 10) as a backstop against an index
generating requirements endlessly under fresh names.

## 6. Install semantics: one transaction

**Choice: the whole plan installs in one database transaction.** Today each
package install is one transaction (`install()` commits at its end). For a
plan, `install()` now:

1. takes a transaction-scoped advisory lock (`sinas:packages`) so concurrent
   installs and uninstalls serialize — otherwise an uninstall of `gmail`
   could commit between a dependent's plan seeing it installed and the
   dependent's commit;
2. reads the root's `package.requires` (with its variables substituted,
   nothing saved) and resolves the plan, fetching what it needs before
   writing anything;
3. refuses if any entry is `missing`, `incompatible` or `cycle`;
4. installs each `install` entry in plan order, then the root, each through
   the same per-package path as before (variables, role governance, config
   apply with `auto_commit=False`, the package record), **without
   committing**;
5. commits once, then flushes every package's queued notifications.

Any failure in step 4 raises before the commit; the caller's session rolls
back and nothing of the plan is left: no dependency rows, no package records,
no secrets saved from secret variables. This is the existing package
all-or-nothing rule widened to the plan, and it needs no recovery procedure.
The cost is one longer transaction, which is fine for an operator action that
is rare and already holds row locks for the duration of an apply.

Within the transaction, a dependency's rows are flushed before the next
package applies, so the dependent's reference checks (an agent referring to
`gmail/api`) find them exactly as they would if `gmail` had been installed
earlier. **References to a dependency's resources are therefore validated at
install by the existing reference checks**, with no new rule.

Packages installed by a plan are recorded with `installed_as_dependency =
true`. An explicit install or update of an installed package leaves the flag
as it was.

Variables: each package in the plan has its own. The install request carries
`dependencyVariables: {<install name>: {VAR: value}}`; the root's stay in
`variables`. `allowBroadRolePermissions` covers every package in the plan (the
preview lists broad permissions per package). `adoptUnmanaged` applies to the
root only: taking over hand-made resources is a decision about one package.

## 7. Preview

The preview (a dry run) returns, beside the root's own result, a
`dependencies` list: the plan entries above, each with its install name,
package name, version, repository, status, constraint(s), who requires it,
and — for `install` entries — that package's own preview: its variable
declarations, `requires_input`, changes, errors and warnings.

Dry runs create nothing, so the root's preview (and a dependency's, for its
own dependencies) can't find the resources a dependency would create. The
plan therefore passes what each planned dependency declares to the next
package's validation and apply as "about to exist" (`ConfigParser.
parse_and_validate(..., extra_names=...)` and `ConfigApplyService(...,
extra_pending_references=...)`), the same mechanism that already lets a
config refer to what it declares itself. Only resources the planned packages
actually declare count, so an undeclared reference still fails the preview as
it would fail the install. Only previews get these; an install has the
dependencies in the database by the time the dependent is validated.

For the same reason the install reads only `package.requires` before
installing the dependencies; the package as a whole is validated after them.

The root's `success` is false (with errors) when any entry is unresolvable,
any dependency's own preview fails, or the plan needs input; the console's
existing "preview failed" gate covers it.

### Upgrades

Installing a package that is already installed is an upgrade:

- Its **new** `requires` are resolved like any install (new dependencies get
  installed, incompatible ones refuse).
- Its **dependents** are checked: every installed package requiring it must
  accept the new version. If one doesn't, the preview lists it (`breaks:
  [{dependent, constraint}]`) and the install is refused unless
  `allowBreakingDependents: true` — the force flag. It is the only override:
  a forced upgrade leaves a dependent whose stated constraint no longer
  holds, but the dependent's resources keep working as long as the
  references hold, and the existing prune check still refuses to remove a
  resource another package's rows use.
- There is **no override for `missing`, `incompatible` or `cycle`**: installing
  a package whose dependencies can't be met would only produce a package
  whose references fail at apply, or one installed without what it needs.

## 8. Uninstall

- **Refused while other installed packages require it** (HTTP 409), listing
  them. Uninstall the dependents first. A cascade option can come later.
- `requires` is stored on the package record (as declared, normalized, with
  the install name each entry targets), so dependents are found from the
  `packages` table without re-parsing YAML: a package `P` is required by every
  installed package with a `requires` entry targeting `P.name`.
- **Orphans** — packages installed as a dependency that nothing requires any
  more — are shown as such (`orphaned: true`) and never removed automatically.
- **Undeclared cross-package references keep today's behaviour**: a package
  that uses another's resources without declaring `requires` installs if they
  exist, and uninstalling the other one is not blocked (the reference
  dangles, as before). Declaring the dependency is how a package gets
  protection; the docs say so.

## 9. Package repositories and the index

The index entry gains an optional `requires` list, as declared:

```json
{"name": "support-mailbox-triage", "version": "1.0.0", ...,
 "requires": [{"name": "gmail", "version": ">=1.2,<2", "repository": "sinas-official"}]}
```

- `build_index.py` emits it only when the package declares requirements, so
  the current index is byte-for-byte unchanged.
- `parse_index` keeps it; malformed entries are dropped, never a reason to
  refuse the index (the same leniency as `multiInstance` and `readme`).
- The catalog API returns `requires` per entry. It is informational: the
  plan always uses the `requires` in the fetched, checksum-verified YAML.

## 10. API

- `POST /packages/preview` and `/install`: new request fields
  `dependencyVariables` and `allowBreakingDependents`. The preview response
  adds `dependencies` (the plan) and `breaks`; the install response adds
  `dependencies` (what was installed).
- `GET /packages` and `GET /packages/{name}`: `requires` (each with its
  resolved `status` and the installed version), `required_by` (install
  names), `installed_as_dependency` and `orphaned`.
- `GET /packages/catalog`: `requires` per entry.
- `DELETE /packages/{name}`: 409 with the dependents when refused.
- The agent tools (`sinas_package_preview` / `_install`) go through the same
  service, so they resolve and install dependencies too; the preview output
  includes the plan.

## 11. Console

- The install dialog lists the plan: satisfied requirements, "Also installs:
  gmail 1.3 from sinas-official", and refusals with the reason. Each package
  to install gets its own configuration section (its variables), before the
  root's; preview again with the values, then confirm installs everything.
- Package details and the installed list show "Requires" (with status) and
  "Required by", and badge packages installed as dependencies / orphaned.
- Uninstall is disabled while the package has dependents, with them listed.
- The catalog shows each package's requirements.

## 12. Data model

`packages` gains:

- `requires JSON NOT NULL DEFAULT '[]'` — the installed version's
  requirements: `[{name, version, repository, instance, install_name}]`.
- `installed_as_dependency BOOLEAN NOT NULL DEFAULT false`.

Existing rows get `[]`/`false`: packages installed before this change
declared no requirements Sinas understood (an unknown `package.requires` key
was ignored). Re-installing such a package records them.

## 13. Out of scope (v1)

- Cascading uninstall, removing orphans.
- Automatic upgrades of an installed dependency to satisfy a new constraint.
- Several versions of a package per index (the index has one).
- Deriving `requires` from manifests.
- Optional dependencies / "recommends".
