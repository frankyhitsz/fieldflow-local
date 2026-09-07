"""Build the offline web UI and provenance inputs into both distributions."""

from pathlib import Path
from shutil import copy2, copytree, rmtree

from setuptools import setup
from setuptools.command.build_py import build_py

ROOT = Path(__file__).parent


class BuildWithOfflineResources(build_py):
    def run(self):
        super().run()
        if self.editable_mode:
            return
        web = ROOT / "frontend" / "dist"
        if not (web / "index.html").exists():
            raise RuntimeError("Run npm --prefix frontend run build before building a distribution")
        package = Path(self.build_lib) / "backend"
        bundled_web = package / "web"
        if bundled_web.exists():
            destination = bundled_web.resolve()
            if any(
                source.resolve() == destination or source.resolve() in destination.parents
                for source in (ROOT / "backend", ROOT / "frontend")
            ):
                raise RuntimeError("build destination must not overwrite source files")
            rmtree(bundled_web)
        copytree(web, bundled_web)
        runtime = package / "_runtime"
        (runtime / "frontend").mkdir(parents=True, exist_ok=True)
        for name in ("pyproject.toml", "requirements-runtime.lock", "docs/sbom-runtime.cdx.json"):
            copy2(ROOT / name, runtime / Path(name).name)
        copy2(ROOT / "frontend/package-lock.json", runtime / "frontend/package-lock.json")


setup(cmdclass={"build_py": BuildWithOfflineResources})
