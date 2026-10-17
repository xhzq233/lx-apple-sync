"""Build the patched alx downloader on macOS, Windows, or Linux."""
import os
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    project = Path(__file__).resolve().parent.parent
    state = Path(os.environ.get("LXSYNC_STATE_DIR", Path.home() / ".local/share/lx-apple-sync")).expanduser().resolve()
    checkout = state / "vendor/agent-lx-music-v0.4.0"
    checkout.parent.mkdir(parents=True, exist_ok=True)
    if not checkout.exists():
        subprocess.run(["git", "clone", "--depth", "1", "--branch", "v0.4.0",
            "https://github.com/Xuepoo/agent-lx-music.git", str(checkout)], check=True)
    for name in ("alx-0.4.0.patch", "alx-0.4.0-daemon.patch"):
        patch = (project / "vendor" / name).read_bytes().replace(b"\r\n", b"\n")
        applied = subprocess.run(["git", "-C", str(checkout), "apply", "--reverse", "--check"], input=patch,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if applied.returncode:
            subprocess.run(["git", "-C", str(checkout), "apply"], input=patch, check=True)
    env = os.environ.copy()
    if sys.platform == "win32" and not env.get("LIBCLANG_PATH"):
        clang = Path(env.get("ProgramFiles", "C:/Program Files")) / "LLVM/bin"
        if (clang / "libclang.dll").is_file():
            env["LIBCLANG_PATH"] = str(clang)
    subprocess.run(["cargo", "build", "--manifest-path", str(checkout / "Cargo.toml"),
        "--release", "--bin", "alx"], env=env, check=True)
    name = "alx.exe" if sys.platform == "win32" else "alx"
    target = Path(os.environ.get("CARGO_TARGET_DIR", checkout / "target"))
    destination = state / "bin" / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(target / "release" / name, destination)
    print(f"alx installed: {destination}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"安装失败：{error}")
