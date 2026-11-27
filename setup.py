import os
from pathlib import Path
import platform
import shutil
import subprocess
from setuptools import setup
from setuptools.command.build_py import build_py as _build_py
from setuptools.command.develop import develop as _develop
from setuptools.command.editable_wheel import editable_wheel as _editable_wheel


def _shared_lib_name() -> str:
    sys_name = platform.system()
    if sys_name == "Windows":
        return "smart_sim.dll"
    if sys_name == "Darwin":
        return "smart_sim.dylib"
    return "smart_sim.so"


def _find_compiler() -> tuple[str, str] | None:
    """Return ('gcc', path) or ('msvc', path) for a 64-bit C compiler, or None."""
    if platform.system() == "Windows":
        for cand in (
            Path(r"C:\msys64\mingw64\bin\gcc.exe"),
            Path(r"C:\msys64\ucrt64\bin\gcc.exe"),
        ):
            if cand.exists():
                return ("gcc", str(cand))

    for tool in ("gcc", "clang"):
        found = shutil.which(tool)
        if found:
            try:
                machine = subprocess.check_output(
                    [found, "-dumpmachine"], text=True, stderr=subprocess.DEVNULL
                ).strip()
                if any(
                    k in machine
                    for k in ("x86_64", "amd64", "aarch64", "arm64", "mingw64", "ucrt", "linux", "darwin")
                ):
                    return ("gcc", found)
            except OSError:
                pass

    if platform.system() == "Windows":
        cl = shutil.which("cl")
        if cl:
            return ("msvc", cl)

    return None


def compile_smart_sim(root_dir: Path) -> None:
    src = root_dir / "native" / "smart_sim.c"
    if not src.exists():
        return

    lib_name = _shared_lib_name()
    repo_out = root_dir / "native" / lib_name
    pkg_native_dir = root_dir / "src" / "netsim" / "native"
    pkg_native_dir.mkdir(parents=True, exist_ok=True)
    pkg_src = pkg_native_dir / "smart_sim.c"
    pkg_out = pkg_native_dir / lib_name

    shutil.copy2(src, pkg_src)

    needs_build = (
        not repo_out.exists()
        or src.stat().st_mtime > repo_out.stat().st_mtime + 1e-6
    )

    if needs_build:
        compiler = _find_compiler()
        if compiler is None:
            print(f"[netsim] Warning: no 64-bit C compiler found; skipping {lib_name} build.")
            return

        kind, cc_path = compiler
        env = os.environ.copy()
        cc_dir = str(Path(cc_path).resolve().parent)
        env["PATH"] = cc_dir + os.pathsep + env.get("PATH", "")

        if kind == "gcc":
            cmd = [cc_path, "-O3", "-shared"]
            if platform.system() != "Windows":
                cmd.append("-fPIC")
            cmd.extend(["-Wall", "-Wextra", str(src), "-o", str(repo_out), "-lm"])
            subprocess.run(cmd, check=True, env=env)
        else:
            cmd = [
                cc_path,
                "/nologo",
                "/O2",
                "/LD",
                str(src),
                f"/Fe:{repo_out}",
            ]
            subprocess.run(cmd, check=True, env=env)

    if repo_out.exists():
        shutil.copy2(repo_out, pkg_out)


class BuildPyWithC(_build_py):
    def run(self) -> None:
        compile_smart_sim(Path(__file__).resolve().parent)
        super().run()


class DevelopWithC(_develop):
    def run(self) -> None:
        compile_smart_sim(Path(__file__).resolve().parent)
        super().run()


class EditableWheelWithC(_editable_wheel):
    def run(self) -> None:
        compile_smart_sim(Path(__file__).resolve().parent)
        super().run()


setup(
    cmdclass={
        "build_py": BuildPyWithC,
        "develop": DevelopWithC,
        "editable_wheel": EditableWheelWithC,
    }
)