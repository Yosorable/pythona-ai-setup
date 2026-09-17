"""Resolve complete MLX dependency sets using local metadata fixtures and the real solver."""

import contextlib
from dataclasses import replace
import importlib.util
import io
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

import resolvelib
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version


MODULE = Path(__file__).resolve().parents[1] / "ai_setup/mlx_dependencies.py"


def load_dependencies():
    spec = importlib.util.spec_from_file_location("mlx_dependencies_under_test", MODULE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class DependencyTests(unittest.TestCase):
    def setUp(self):
        self.dependencies = load_dependencies()
        self.enterContext(patch.object(self.dependencies, "sys", types.SimpleNamespace(
            platform="ios", version_info=sys.version_info, base_prefix=sys.base_prefix, modules=sys.modules)))
        self.environment = default_environment()
        self.environment.update(sys_platform="ios", platform_system="iOS",
                                platform_machine="iPhone15,2", python_full_version="3.14.0",
                                python_version="3.14")
        self.bundled = {}
        for name, version, requirements in (
            ("mlx", "0.32.0", ('mlx-metal==0.32.0; platform_system == "Darwin"',)),
            ("tokenizers", "0.22.2", ("huggingface-hub<2",)),
            ("numpy", "2.5.1", ()),
            ("packaging", "26.3", ()),
            ("typing-extensions", "4.16.0", ()),
            ("markupsafe", "3.0.3", ()),
        ):
            self.bundled[name] = self.candidate(name, version, requirements, bundled=True)
        self.installed = dict(self.bundled)
        self.releases = {}
        self.release("mlx-lm", "0.31.3", [
            'mlx>=0.31.2; platform_system == "Darwin"', "transformers>=5", "jinja2",
        ])
        self.release("mlx-lm", "0.32.0", [
            'mlx>=0.33; platform_system == "Darwin"', "transformers>=5", "jinja2",
        ])
        self.release("transformers", "5.14.1", [
            "tokenizers>=0.22,<=0.23", "huggingface-hub>=1", "numpy>=2",
        ])
        self.release("transformers", "5.17.0", [
            "tokenizers>=0.23.1,<0.24", "huggingface-hub>=1", "numpy>=2",
        ])
        hub_requirements = [
            "httpx[socks]>=0.28", "typing-extensions>=4.12",
            'hf-xet; platform_machine == "aarch64" or platform_machine == "arm64"',
        ]
        self.release("huggingface-hub", "1.32.0", hub_requirements)
        self.release("huggingface-hub", "2.0.0", hub_requirements)
        self.release("httpx", "0.28.1", [
            "idna", 'socksio==1; extra == "socks"', 'win32-only; sys_platform == "win32"',
        ])
        self.release("idna", "3.19", [])
        self.release("socksio", "1.0", [])
        self.release("jinja2", "3.1.6", ["markupsafe>=2"])

        self.index = self.dependencies.MLXPyPIIndex(self.environment)
        self.fetch = Mock(side_effect=self.index_metadata)
        self.index.metadata = self.fetch
        self.fail_on = None
        self.changed_batches = []
        self.installer = Mock(side_effect=self.install_many)
        pythona = types.ModuleType("pythona")
        pythona.packages = types.SimpleNamespace(
            install=Mock(side_effect=AssertionError("Inference dependencies must use the batch API")), install_many=self.installer)
        self.enterContext(patch.dict(sys.modules, {"pythona": pythona}))
        self.enterContext(patch.object(self.dependencies, "mlx_load_resolver", return_value=resolvelib))
        self.enterContext(patch.object(self.dependencies, "MLXPyPIIndex", return_value=self.index))
        self.enterContext(patch("packaging.markers.default_environment", return_value=self.environment))
        self.distributions = self.enterContext(patch.object(
            self.dependencies.metadata, "distributions", side_effect=self.distribution_list))
        self.enterContext(patch.object(self.dependencies.metadata, "version", side_effect=self.version))
        self.module_distributions = self.enterContext(patch.object(
            self.dependencies.metadata, "packages_distributions", return_value={}))
        self.output = self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def candidate(self, name, version, requirements=(), *, bundled=False):
        return self.dependencies.MLXDependencyCandidate(name, Version(version), tuple(requirements), bundled=bundled)

    def release(self, name, version, requirements, *, python=">=3.9", yanked=False, tag="py3-none-any"):
        file = {"filename": f"{name.replace('-', '_')}-{version}-{tag}.whl",
                "packagetype": "bdist_wheel", "requires_python": python, "yanked": yanked}
        self.releases.setdefault(name, {})[version] = {
            "info": {"name": name, "version": version, "requires_dist": requirements, "requires_python": python},
            "urls": [file],
        }

    def index_metadata(self, name, version=None):
        versions = self.releases.get(name, {})
        if not versions:
            return {"info": {"name": name, "version": "0"}, "releases": {}}
        if version is not None:
            return versions[version]
        latest = max(versions, key=Version)
        return {**versions[latest],
                "releases": {version: data["urls"] for version, data in versions.items()}}

    def distribution_list(self, *, path=None):
        candidates = self.bundled if path is not None else self.installed
        return [types.SimpleNamespace(metadata={"Name": c.name}, version=str(c.version),
                                      requires=c.requirements) for c in candidates.values()]

    def version(self, name):
        if name not in self.installed:
            raise self.dependencies.metadata.PackageNotFoundError(name)
        return str(self.installed[name].version)

    def install_many(self, requirements):
        target = {}
        for text in requirements:
            requirement = Requirement(text)
            specifiers = list(requirement.specifier)
            self.assertEqual(len(specifiers), 1)
            self.assertEqual(specifiers[0].operator, "==")
            name, version = canonicalize_name(requirement.name), specifiers[0].version
            if name in self.bundled:
                candidate = self.bundled[name]
                self.assertEqual(candidate.version, Version(version))
            else:
                candidate = self.candidate(name, version, self.releases[name][version]["info"]["requires_dist"])
            target[name] = replace(candidate, extras=frozenset(requirement.extras))
        # Verify the complete pinned graph, including extras and unchanged packages.
        for candidate in target.values():
            if candidate.bundled:
                continue
            for requirement in self.dependencies.mlx_active_requirements(candidate, self.environment):
                dependency = target.get(canonicalize_name(requirement.name))
                self.assertIsNotNone(dependency, f"Missing pin for {requirement}")
                self.assertIn(dependency.version, requirement.specifier)
                self.assertTrue(requirement.extras.issubset(dependency.extras))
        changed = {name for name, candidate in target.items() if name not in self.installed
                   or self.installed[name].version != candidate.version}
        for name, retained in self.installed.items():
            if name in changed:
                continue
            for requirement in self.dependencies.mlx_active_requirements(retained, self.environment):
                dependency = canonicalize_name(requirement.name)
                if dependency in changed and target[dependency].version not in requirement.specifier:
                    raise RuntimeError(f"{name} requires {requirement}")
        if self.fail_on in changed:
            raise RuntimeError("Download interrupted")
        self.changed_batches.append(changed)
        self.installed.update(target)

    def resolve(self):
        return self.dependencies.mlx_resolve_dependencies(self.bundled, self.installed, self.environment, self.index)

    def selected(self):
        return {candidate.name: str(candidate.version) for candidate in self.resolve()}

    def test_import_does_not_install_or_inspect_packages(self):
        self.distributions.reset_mock()
        load_dependencies()
        self.distributions.assert_not_called()
        self.installer.assert_not_called()

    def test_embedded_backend_prepares_the_same_complete_dependency_set(self):
        from ai_setup.bundle import load_backend

        backend = load_backend()
        backend.update(mlx_load_resolver=lambda: resolvelib, MLXPyPIIndex=lambda _: self.index,
                       sys=self.dependencies.sys)
        backend["prepare_mlx_dependencies"]()
        self.installer.assert_called_once()
        requirements = self.installer.call_args.args[0]
        self.assertIn("mlx-lm==0.31.3", requirements)
        self.assertIn("transformers==5.14.1", requirements)
        self.assertIn("httpx[socks]==0.28.1", requirements)

    def test_desktop_environment_is_not_managed_by_the_ios_installer(self):
        with patch.object(self.dependencies.sys, "platform", "darwin"):
            self.dependencies.prepare_mlx_dependencies()
        self.distributions.assert_not_called()
        self.fetch.assert_not_called()
        self.installer.assert_not_called()

    def test_backtracks_transformers_and_mlx_lm_against_bundled_native_versions(self):
        selected = self.selected()
        self.assertEqual(selected["transformers"], "5.14.1")
        self.assertEqual(selected["mlx-lm"], "0.31.3")
        self.assertEqual(selected["huggingface-hub"], "1.32.0")
        self.assertNotIn("mlx-metal", selected)
        self.assertNotIn("hf-xet", selected)

    def test_new_bundled_versions_select_new_releases_without_a_version_table(self):
        self.bundled["mlx"] = replace(self.bundled["mlx"], version=Version("0.33"))
        self.bundled["tokenizers"] = replace(self.bundled["tokenizers"], version=Version("0.23.1"))
        self.installed.update(self.bundled)
        selected = self.selected()
        self.assertEqual(selected["mlx-lm"], "0.32.0")
        self.assertEqual(selected["transformers"], "5.17.0")

    def test_reads_fixed_versions_from_bundle_path_instead_of_user_packages(self):
        self.installed["tokenizers"] = self.candidate("tokenizers", "99")
        bundled = self.dependencies.mlx_bundled_candidates()
        self.assertEqual(str(bundled["tokenizers"].version), "0.22.2")
        path = self.distributions.call_args.kwargs["path"][0]
        self.assertEqual(path, str(Path(sys.base_prefix) / "lib" /
                                  f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"))

    def test_missing_mlx_stops_before_network_or_installation(self):
        del self.bundled["mlx"]
        with self.assertRaisesRegex(RuntimeError, "bundled mlx"):
            self.dependencies.prepare_mlx_dependencies()
        self.fetch.assert_not_called()
        self.installer.assert_not_called()

    def test_conflicting_user_override_is_not_used_as_the_native_constraint(self):
        self.installed["tokenizers"] = self.candidate("tokenizers", "0.23.1")
        with self.assertRaisesRegex(RuntimeError, "overrides bundled tokenizers 0.22.2"):
            self.dependencies.prepare_mlx_dependencies()
        self.installer.assert_not_called()

    def test_first_install_submits_one_complete_plan_with_exact_versions_and_extras(self):
        self.dependencies.prepare_mlx_dependencies()
        self.assertEqual(self.version("transformers"), "5.14.1")
        self.assertEqual(self.version("mlx-lm"), "0.31.3")
        self.assertEqual(self.version("socksio"), "1.0")
        self.installer.assert_called_once()
        requirements = self.installer.call_args.args[0]
        self.assertIn("httpx[socks]==0.28.1", requirements)
        self.assertIn("tokenizers==0.22.2", requirements)
        self.assertTrue(self.changed_batches[0].isdisjoint(self.bundled))

    def test_compatible_installed_graph_works_without_network_or_installs(self):
        self.dependencies.prepare_mlx_dependencies()
        self.fetch.reset_mock()
        self.fetch.side_effect = AssertionError("A compatible environment must not query PyPI")
        self.installer.reset_mock()
        self.dependencies.prepare_mlx_dependencies()
        self.fetch.assert_not_called()
        self.installer.assert_not_called()

    def test_new_native_constraint_repairs_an_installed_incompatible_graph(self):
        self.dependencies.prepare_mlx_dependencies()
        self.bundled["mlx"] = replace(self.bundled["mlx"], version=Version("0.33"))
        self.bundled["tokenizers"] = replace(self.bundled["tokenizers"], version=Version("0.23.1"))
        self.installed.update(self.bundled)
        self.installer.reset_mock()
        self.dependencies.prepare_mlx_dependencies()
        self.assertEqual(self.version("transformers"), "5.17.0")
        # Reuse MLX-LM while its installed version is still compatible.
        self.assertEqual(self.version("mlx-lm"), "0.31.3")
        self.installer.assert_called_once()
        self.assertEqual(self.changed_batches[-1], {"transformers"})
        self.assertIn("mlx-lm==0.31.3", self.installer.call_args.args[0])

    def test_app_upgrade_can_replace_a_parent_and_child_in_the_same_plan(self):
        self.releases["mlx-lm"]["0.31.3"]["info"]["requires_dist"] = [
            'mlx>=0.31.2; platform_system == "Darwin"', "transformers>=5,<5.15", "jinja2"]
        self.dependencies.prepare_mlx_dependencies()
        self.assertEqual(self.version("mlx-lm"), "0.31.3")
        self.assertEqual(self.version("transformers"), "5.14.1")
        self.bundled["mlx"] = replace(self.bundled["mlx"], version=Version("0.33"))
        self.bundled["tokenizers"] = replace(self.bundled["tokenizers"], version=Version("0.23.1"))
        self.installed.update(self.bundled)
        self.installer.reset_mock()
        self.dependencies.prepare_mlx_dependencies()
        self.installer.assert_called_once()
        self.assertEqual(self.changed_batches[-1], {"mlx-lm", "transformers"})
        self.assertEqual(self.version("mlx-lm"), "0.32.0")
        self.assertEqual(self.version("transformers"), "5.17.0")

    def test_old_app_without_batch_api_reports_an_update_requirement(self):
        with patch.object(sys.modules["pythona"], "packages", types.SimpleNamespace(install=Mock())):
            with self.assertRaisesRegex(RuntimeError, "Update Pythona"):
                self.dependencies.prepare_mlx_dependencies()
        self.installer.assert_not_called()

    def test_incompatible_newer_installed_transformers_can_be_downgraded(self):
        self.installed["transformers"] = self.candidate(
            "transformers", "5.17.0", self.releases["transformers"]["5.17.0"]["info"]["requires_dist"])
        self.dependencies.prepare_mlx_dependencies()
        self.assertEqual(self.version("transformers"), "5.14.1")

    def test_backtracks_across_sibling_transitive_constraints(self):
        for data in self.releases["mlx-lm"].values():
            data["info"]["requires_dist"] += ["left", "right"]
        self.release("left", "2", ["common>=2"])
        self.release("left", "1", ["common>=1"])
        self.release("right", "1", ["common<2"])
        self.release("common", "2", [])
        self.release("common", "1", [])
        selected = self.selected()
        self.assertEqual(selected["left"], "1")
        self.assertEqual(selected["common"], "1")

    def test_extras_added_by_a_later_parent_expand_dependencies(self):
        data = self.releases["huggingface-hub"]["1.32.0"]["info"]
        data["requires_dist"] = ["httpx>=0.28", "transport"]
        self.release("transport", "1", ["httpx[socks]>=0.28"])
        self.assertIn("socksio", self.selected())

    def test_bundled_reverse_constraint_can_request_an_extra(self):
        data = self.releases["huggingface-hub"]["1.32.0"]["info"]
        data["requires_dist"] = ["httpx>=0.28"]
        self.bundled["tokenizers"] = replace(
            self.bundled["tokenizers"], requirements=("huggingface-hub<2", "httpx[socks]>=0.28"))
        self.assertIn("socksio", self.selected())

    def test_incompatible_python_yanked_prerelease_and_native_wheels_are_skipped(self):
        dependencies = self.releases["transformers"]["5.14.1"]["info"]["requires_dist"]
        self.release("transformers", "98", dependencies, python=">=99")
        self.release("transformers", "97", dependencies, yanked=True)
        self.release("transformers", "96rc1", dependencies)
        self.release("transformers", "95", dependencies, tag="cp314-cp314-macosx_14_0_arm64")
        self.assertEqual(self.selected()["transformers"], "5.14.1")

    def test_native_only_dependency_causes_backtracking(self):
        dependencies = self.releases["transformers"]["5.14.1"]["info"]["requires_dist"]
        self.release("transformers", "6", dependencies + ["native-only"])
        self.release("native-only", "1", [], tag="cp314-cp314-macosx_14_0_arm64")
        self.assertEqual(self.selected()["transformers"], "5.14.1")

    def test_direct_url_dependency_causes_backtracking(self):
        dependencies = self.releases["transformers"]["5.14.1"]["info"]["requires_dist"]
        self.release("transformers", "6", dependencies + ["remote @ https://example.com/remote.whl"])
        self.assertEqual(self.selected()["transformers"], "5.14.1")

    def test_metadata_network_failure_stops_before_installation(self):
        self.fetch.side_effect = RuntimeError("PyPI temporarily unavailable")
        with self.assertRaisesRegex(RuntimeError, "PyPI temporarily unavailable"):
            self.dependencies.prepare_mlx_dependencies()
        self.installer.assert_not_called()

    def test_no_solution_does_not_start_inference_package_installation(self):
        self.bundled["tokenizers"] = replace(self.bundled["tokenizers"], version=Version("0.10"))
        self.installed.update(self.bundled)
        with self.assertRaisesRegex(RuntimeError, "No compatible dependency set"):
            self.dependencies.prepare_mlx_dependencies()
        self.installer.assert_not_called()

    def test_batch_plan_can_include_a_valid_dependency_cycle(self):
        self.releases["mlx-lm"]["0.31.3"]["info"]["requires_dist"].append("loop-a")
        self.release("loop-a", "1", ["loop-b"])
        self.release("loop-b", "1", ["loop-a"])
        self.dependencies.prepare_mlx_dependencies()
        self.installer.assert_called_once()
        self.assertEqual(self.version("loop-a"), "1")
        self.assertEqual(self.version("loop-b"), "1")

    def test_failed_batch_download_can_retry_with_the_old_environment(self):
        self.fail_on = "huggingface-hub"
        before = dict(self.installed)
        with self.assertRaisesRegex(RuntimeError, "Download interrupted"):
            self.dependencies.prepare_mlx_dependencies()
        self.assertEqual(self.installed, before)
        self.fail_on = None
        self.installer.reset_mock()
        self.dependencies.prepare_mlx_dependencies()
        self.installer.assert_called_once()
        self.assertEqual(self.version("mlx-lm"), "0.31.3")

    def test_installer_failure_does_not_bypass_shared_package_checks(self):
        self.installer.side_effect = RuntimeError("another-project requires idna<3")
        with self.assertRaisesRegex(RuntimeError, "another-project requires idna<3"):
            self.dependencies.prepare_mlx_dependencies()
        self.assertNotIn("mlx-lm", self.installed)

    def test_installer_must_install_the_requested_version(self):
        self.installer.side_effect = lambda requirements: None
        with self.assertRaisesRegex(RuntimeError, "Dependency changed during setup"):
            self.dependencies.prepare_mlx_dependencies()

    def test_unexpected_change_to_an_earlier_dependency_is_detected(self):
        def install_and_replace(requirements):
            self.install_many(requirements)
            self.installed["idna"] = self.candidate("idna", "99")

        self.installer.side_effect = install_and_replace
        with self.assertRaisesRegex(RuntimeError, "Dependency changed during setup: idna"):
            self.dependencies.prepare_mlx_dependencies()

    def test_loaded_package_is_not_replaced_in_the_running_interpreter(self):
        self.installed["transformers"] = self.candidate(
            "transformers", "5.17.0", self.releases["transformers"]["5.17.0"]["info"]["requires_dist"])
        module = types.ModuleType("transformers")
        self.module_distributions.return_value = {"transformers": ["transformers"]}
        with patch.dict(sys.modules, {"transformers": module}):
            with self.assertRaisesRegex(RuntimeError, "Restart Pythona"):
                self.dependencies.prepare_mlx_dependencies()
        self.installer.assert_not_called()

    def test_loaded_stale_version_is_detected_even_when_files_already_match(self):
        self.dependencies.prepare_mlx_dependencies()
        self.installer.reset_mock()
        module = types.ModuleType("transformers")
        module.__version__ = "4.0"
        self.module_distributions.return_value = {"transformers": ["transformers"]}
        with patch.dict(sys.modules, {"transformers": module}):
            with self.assertRaisesRegex(RuntimeError, "Restart Pythona"):
                self.dependencies.prepare_mlx_dependencies()
        self.installer.assert_not_called()

    def test_equivalent_loaded_version_does_not_require_restart(self):
        candidate = self.candidate("certifi", "2026.7.22")
        module = types.ModuleType("certifi")
        module.__version__ = "2026.07.22"
        self.module_distributions.return_value = {"certifi": ["certifi"]}
        with patch.dict(sys.modules, {"certifi": module}):
            self.dependencies.mlx_check_loaded_packages([candidate], {"certifi": candidate})

class ResolverBootstrapTests(unittest.TestCase):
    def test_first_run_installs_only_the_solver_with_a_fixed_api_version(self):
        dependencies = load_dependencies()
        solver = types.ModuleType("resolvelib")
        solver.__version__ = "1.2.1"
        installer = Mock(side_effect=lambda *args, **kwargs: sys.modules.update(resolvelib=solver))
        pythona = types.ModuleType("pythona")
        pythona.packages = types.SimpleNamespace(install=installer)
        with patch.dict(sys.modules, {"pythona": pythona}):
            sys.modules.pop("resolvelib", None)
            with patch.object(dependencies, "mlx_installed_version", side_effect=[None, "1.2.1"]):
                self.assertIs(dependencies.mlx_load_resolver(), solver)
        installer.assert_called_once_with("resolvelib", version="1.2.1")

    def test_loaded_incompatible_solver_requires_restart(self):
        dependencies = load_dependencies()
        solver = types.ModuleType("resolvelib")
        solver.__version__ = "1.1.0"
        with patch.dict(sys.modules, {"resolvelib": solver}):
            with patch.object(dependencies, "mlx_installed_version", return_value="1.2.1"):
                with self.assertRaisesRegex(RuntimeError, "Restart Pythona"):
                    dependencies.mlx_load_resolver()


class PyPIMetadataTests(unittest.TestCase):
    def test_unknown_package_is_unavailable_instead_of_aborting_backtracking(self):
        dependencies = load_dependencies()
        index = dependencies.MLXPyPIIndex(default_environment())
        error = dependencies.HTTPError("https://pypi.org/pypi/missing/json", 404, "Not Found", {}, None)
        with patch.object(dependencies, "urlopen", side_effect=error) as fetch:
            self.assertEqual(list(index.candidates("missing", [], frozenset())), [])
            self.assertEqual(list(index.candidates("missing", [], frozenset())), [])
        self.assertEqual(fetch.call_count, 1)

    def test_network_timeout_is_not_treated_as_an_unavailable_version(self):
        dependencies = load_dependencies()
        index = dependencies.MLXPyPIIndex(default_environment())
        with patch.object(dependencies, "urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaisesRegex(RuntimeError, "Check your connection"):
                list(index.candidates("mlx-lm", [], frozenset()))


if __name__ == "__main__":
    unittest.main()
