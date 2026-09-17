"""Resolve MLX dependencies against the current iOS bundle before importing model SDKs."""

from dataclasses import dataclass, replace
import importlib
from importlib import metadata
import json
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


# A tested minimum for the backend API; dependency versions follow the current bundle.
MLX_LM_REQUIREMENT = "mlx-lm>=0.31.3"


@dataclass(frozen=True)
class MLXDependencyCandidate:
    name: str
    version: object
    requirements: tuple[str, ...] = ()
    extras: frozenset[str] = frozenset()
    bundled: bool = False


def mlx_installed_version(name):
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def mlx_distribution_candidates(distributions, *, bundled=False):
    from packaging.utils import canonicalize_name
    from packaging.version import Version

    result = {}
    for dist in distributions:
        name = canonicalize_name(dist.metadata["Name"])
        # Preserve sys.path precedence when a user package shadows a bundle.
        result.setdefault(name, MLXDependencyCandidate(
            name, Version(dist.version), tuple(dist.requires or ()), bundled=bundled))
    return result


def mlx_bundled_candidates():
    python_dir = f"python{sys.version_info.major}.{sys.version_info.minor}"
    path = Path(sys.base_prefix) / "lib" / python_dir / "site-packages"
    bundled = mlx_distribution_candidates(metadata.distributions(path=[str(path)]), bundled=True)
    for name in ("mlx", "tokenizers", "packaging"):
        if name not in bundled:
            raise RuntimeError(f"Pythona's bundled {name} is required. Use MLX on a supported device.")
    return bundled


def mlx_active_requirements(candidate, environment):
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    for text in candidate.requirements:
        requirement = Requirement(text)
        marker_environment = environment
        # MLX-LM marks its Apple MLX requirement as Darwin. Pythona supplies
        # MLX on iOS too, so this constraint must still protect the native API.
        if (candidate.name == "mlx-lm" and canonicalize_name(requirement.name) == "mlx"
                and environment["sys_platform"] == "ios"):
            marker_environment = {**environment, "platform_system": "Darwin"}
        if requirement.marker is None or any(
            requirement.marker.evaluate({**marker_environment, "extra": extra})
            for extra in ("", *candidate.extras)
        ):
            yield requirement


class MLXPyPIIndex:
    def __init__(self, environment):
        self.environment = environment
        self.cache = {}

    def metadata(self, name, version=None):
        from packaging.utils import canonicalize_name
        from packaging.version import Version

        key = (name, version)
        if key not in self.cache:
            path = "/".join(quote(part, safe="") for part in (name, version) if part is not None)
            request = Request(f"https://pypi.org/pypi/{path}/json",
                              headers={"User-Agent": "Pythona-AI-Setup"})
            try:
                with urlopen(request, timeout=30) as response:
                    data = json.load(response)
            except HTTPError as error:
                error.close()
                if error.code == 404:
                    self.cache[key] = None
                    return None
                raise RuntimeError(f"Cannot read PyPI metadata for {name}: {error}") from error
            except (URLError, OSError, ValueError) as error:
                raise RuntimeError(f"Cannot read PyPI metadata for {name}. Check your connection and retry: {error}") from error
            info = data["info"]
            if (canonicalize_name(info["name"]) != name
                    or (version is not None and Version(info["version"]) != Version(version))):
                raise RuntimeError(f"Unexpected PyPI metadata for {name} {version or ''}.")
            self.cache[key] = data
        return self.cache[key]

    def supports_python(self, specifier):
        from packaging.specifiers import InvalidSpecifier, SpecifierSet

        try:
            return SpecifierSet(specifier or "").contains(
                self.environment["python_full_version"], prereleases=True)
        except InvalidSpecifier:
            return False

    def supports_wheel(self, file, name, version):
        from packaging.utils import InvalidWheelFilename, parse_wheel_filename

        if file.get("packagetype") != "bdist_wheel" or file.get("yanked"):
            return False
        try:
            wheel_name, wheel_version, _, tags = parse_wheel_filename(file["filename"])
        except InvalidWheelFilename:
            return False
        # Match the App installer's supported wheel format.
        return (wheel_name == name and wheel_version == version
                and any(tag.interpreter == "py3" and tag.abi == "none" and tag.platform == "any"
                        for tag in tags)
                and self.supports_python(file.get("requires_python")))

    def candidates(self, name, requirements, extras):
        from packaging.version import InvalidVersion, Version

        index = self.metadata(name)
        if index is None:
            return
        releases = []
        for raw, files in index.get("releases", {}).items():
            try:
                version = Version(raw)
            except InvalidVersion:
                continue
            if version.is_prerelease or version.is_devrelease:
                continue
            if (all(req.specifier.contains(version, prereleases=True) for req in requirements)
                    and any(self.supports_wheel(file, name, version) for file in files)):
                releases.append((version, raw))
        for version, raw in sorted(releases, reverse=True):
            data = index if Version(index["info"]["version"]) == version else self.metadata(name, raw)
            if data is None:
                continue
            info = data["info"]
            if self.supports_python(info.get("requires_python")):
                yield MLXDependencyCandidate(name, version, tuple(info.get("requires_dist") or ()), extras)


class MLXDependencyProvider:
    def __init__(self, bundled, installed, environment, index):
        self.bundled = bundled
        self.installed = installed
        self.environment = environment
        self.index = index
        self.bundle_constraints = {}
        # Honor reverse constraints such as tokenizers -> huggingface-hub,
        # without trying to install platform components of bundled libraries.
        for candidate in bundled.values():
            for requirement in mlx_active_requirements(candidate, environment):
                self.bundle_constraints.setdefault(self.identify(requirement), []).append(requirement)

    def identify(self, requirement_or_candidate):
        from packaging.utils import canonicalize_name

        return canonicalize_name(requirement_or_candidate.name)

    def narrow_requirement_selection(self, identifiers, **kwargs):
        return identifiers

    def get_preference(self, identifier, **kwargs):
        order = {"mlx-lm": 0, "transformers": 1, "huggingface-hub": 2}
        return (identifier not in self.bundled, order.get(identifier, 3), identifier)

    def is_satisfied_by(self, requirement, candidate):
        return (requirement.url is None
                and requirement.specifier.contains(candidate.version, prereleases=True)
                and set(requirement.extras).issubset(candidate.extras))

    def find_matches(self, identifier, requirements, incompatibilities):
        requested = list(requirements[identifier])
        constraints = requested + self.bundle_constraints.get(identifier, [])
        extras = frozenset(extra for req in constraints for extra in req.extras)
        excluded = set(incompatibilities[identifier])

        def matches():
            if any(req.url is not None for req in constraints):
                return
            fixed = self.bundled.get(identifier)
            existing = fixed or self.installed.get(identifier)
            if existing is not None:
                existing = replace(existing, extras=extras)
                if (existing not in excluded
                        and all(req.specifier.contains(existing.version, prereleases=True) for req in constraints)):
                    yield existing
            if fixed is not None:
                return
            for candidate in self.index.candidates(identifier, constraints, extras):
                if candidate not in excluded and (existing is None or candidate.version != existing.version):
                    yield candidate

        # A factory keeps PyPI requests lazy: compatible installed packages
        # resolve without a network request, including on later offline runs.
        return matches

    def get_dependencies(self, candidate):
        return [] if candidate.bundled else list(mlx_active_requirements(candidate, self.environment))


def mlx_load_resolver():
    from packaging.specifiers import SpecifierSet
    from packaging.version import Version

    # Only the solver's API is fixed; MLX-LM and its dependency versions are
    # resolved from the current bundle. This helper is an 18 KB Python wheel.
    version = mlx_installed_version("resolvelib")
    if version is None or Version(version) not in SpecifierSet(">=1.2.1,<2"):
        if "resolvelib" in sys.modules:
            raise RuntimeError("Restart Pythona before updating the already imported resolvelib.")
        from pythona import packages

        packages.install("resolvelib", version="1.2.1")
        importlib.invalidate_caches()
        if mlx_installed_version("resolvelib") != "1.2.1":
            raise RuntimeError("The dependency resolver was not installed successfully. Retry the model request.")
    import resolvelib

    if Version(resolvelib.__version__) not in SpecifierSet(">=1.2.1,<2"):
        raise RuntimeError("An older dependency resolver is still imported. Restart Pythona and retry.")
    return resolvelib


def mlx_resolve_dependencies(bundled, installed, environment, index):
    from packaging.requirements import Requirement

    solver = mlx_load_resolver()
    provider = MLXDependencyProvider(bundled, installed, environment, index)
    try:
        result = solver.Resolver(provider, solver.BaseReporter()).resolve(
            [Requirement(MLX_LM_REQUIREMENT)], max_rounds=1000)
    except solver.ResolutionImpossible as error:
        details = "\n".join(sorted({
            f"  {cause.parent.name if cause.parent else 'MLX backend'} requires {cause.requirement}"
            for cause in error.causes
        }))
        raise RuntimeError("No compatible dependency set was found for the bundled libraries.\n" + details) from error
    except solver.ResolutionTooDeep as error:
        raise RuntimeError("Dependency resolution reached its search limit. No inference packages were changed.") from error
    return sorted(result.mapping.values(), key=lambda candidate: candidate.name)


def mlx_pinned_requirement(candidate):
    extras = "[" + ",".join(sorted(candidate.extras)) + "]" if candidate.extras else ""
    return f"{candidate.name}{extras}=={candidate.version}"


def mlx_check_loaded_packages(plan, installed):
    from packaging.utils import canonicalize_name
    from packaging.version import InvalidVersion, Version

    loaded_roots = {name.partition(".")[0] for name in sys.modules}
    selected = {candidate.name: candidate for candidate in plan}
    for module_name, distributions in metadata.packages_distributions().items():
        if module_name not in loaded_roots:
            continue
        for name in distributions:
            name = canonicalize_name(name)
            candidate = selected.get(name)
            if candidate is None:
                continue
            current = installed.get(name)
            module = sys.modules.get(module_name)
            module_version = vars(module).get("__version__") if module is not None else None
            try:
                loaded_version = Version(module_version) if isinstance(module_version, str) else None
            except InvalidVersion:
                loaded_version = None
            if (current is not None and current.version != candidate.version
                    or loaded_version is not None and loaded_version != candidate.version):
                raise RuntimeError(
                    f"{name} is already imported with a different version. Restart Pythona, "
                    "then retry the model request before importing it in another script.")


def prepare_mlx_dependencies():
    # Desktop MLX checks use an environment managed outside Pythona.
    if sys.platform != "ios":
        return
    from packaging.markers import default_environment
    from packaging.version import Version

    bundled = mlx_bundled_candidates()
    installed = mlx_distribution_candidates(metadata.distributions())
    environment = default_environment()
    plan = mlx_resolve_dependencies(bundled, installed, environment, MLXPyPIIndex(environment))

    for candidate in plan:
        visible = installed.get(candidate.name)
        if candidate.bundled and (visible is None or visible.version != candidate.version):
            raise RuntimeError(
                f"A user package overrides bundled {candidate.name} {candidate.version}. "
                "Remove the overriding user package in the package manager, then restart Pythona.")
    mlx_check_loaded_packages(plan, installed)

    pending = [candidate for candidate in plan if not candidate.bundled
               and (candidate.name not in installed or installed[candidate.name].version != candidate.version)]
    if pending:
        from pythona import packages

        install_many = getattr(packages, "install_many", None)
        if install_many is None:
            raise RuntimeError("Update Pythona to install the coordinated MLX dependency set.")
        print("Compatible Python dependencies:\n" + "\n".join(
            f"  {candidate.name}=={candidate.version}" for candidate in pending))
        # Include unchanged and bundled versions, as well as requested extras,
        # so the installer keeps the entire resolved set in the same plan.
        try:
            install_many([mlx_pinned_requirement(candidate) for candidate in plan])
        except RuntimeError as error:
            raise RuntimeError(
                f"Could not install the compatible dependency set: {error}\n"
                "Review any shared packages named in the error before retrying. "
                "Reinstalling the provider does not reset shared Python packages.") from error
        importlib.invalidate_caches()

    for candidate in plan:
        actual = mlx_installed_version(candidate.name)
        if actual is None or Version(actual) != candidate.version:
            raise RuntimeError(f"Dependency changed during setup: {candidate.name} {actual}; expected {candidate.version}.")
