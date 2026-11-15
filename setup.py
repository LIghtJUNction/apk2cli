"""Include the canonical project skills in built distributions."""

from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildWithSkills(build_py):
    def run(self):
        super().run()
        root = Path(__file__).parent
        target = Path(self.build_lib) / "apk2cli" / "resources"
        shutil.copytree(root / ".agents" / "skills", target / "skills", dirs_exist_ok=True)
        (target / "docs").mkdir(parents=True, exist_ok=True)
        for name in ("references.md", "specification.md", "mcp.example.json"):
            source = root / "docs" / name
            if source.is_file():
                shutil.copy2(source, target / "docs" / name)


setup(cmdclass={"build_py": BuildWithSkills})
