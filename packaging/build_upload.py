#!/usr/bin/env python3
"""! @file
@brief Build the self-contained cim-upload executable: upload.py, Python itself,
`cryptography` and the Remote access link client in one file, so a machine with
nothing installed can upload, at home or over the direct link.

    python3 packaging/build_upload.py            # -> dist/cim-upload (cim-upload.exe on Windows)

Builds for the machine it runs on (PyInstaller does not cross-compile): run it
once per OS / CPU you want a binary for (the upload-binary workflow does that on
every tag). A Linux binary runs on systems with the same or a newer glibc than
the build machine, so build on an old distro for wide reach.

Everything happens in a throwaway venv under build/upload/; nothing touches the
app's own venv.
"""

import os
import platform
import shutil
import subprocess
import sys
import venv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(ROOT, "build", "upload")
DIST = os.path.join(ROOT, "dist")
NAME = "cim-upload"
REQS = ["pyinstaller>=6.6", "cryptography>=42"]
# link.py / dht.py are loaded by path at run time (upload.py, load_link), so
# PyInstaller can't see what they import: name it.
HIDDEN = ["cryptography.exceptions",
          "cryptography.hazmat.primitives.hashes",
          "cryptography.hazmat.primitives.serialization",
          "cryptography.hazmat.primitives.asymmetric.x25519",
          "cryptography.hazmat.primitives.asymmetric.ed25519",
          "cryptography.hazmat.primitives.ciphers.aead",
          "cryptography.hazmat.primitives.kdf.hkdf"]


def run(cmd):
    print("+ " + " ".join(cmd), flush=True)
    subprocess.check_call(cmd)


def main():
    env_dir = os.path.join(WORK, "venv")
    py = os.path.join(env_dir, "Scripts" if os.name == "nt" else "bin", "python" + (".exe" if os.name == "nt" else ""))
    if not os.path.exists(py):
        venv.EnvBuilder(with_pip=True, clear=True).create(env_dir)
    run([py, "-m", "pip", "install", "-q", "--upgrade", "pip"])
    run([py, "-m", "pip", "install", "-q"] + REQS)

    link_dir = os.path.join(ROOT, "modules", "remote_access")
    args = [py, "-m", "PyInstaller", "--onefile", "--clean", "--noconfirm", "--name", NAME,
            "--distpath", DIST, "--workpath", os.path.join(WORK, "pyi"), "--specpath", WORK,
            "--add-data", os.path.join(link_dir, "link.py") + os.pathsep + "cimlink",
            "--add-data", os.path.join(link_dir, "dht.py") + os.pathsep + "cimlink"]
    for h in HIDDEN:
        args += ["--hidden-import", h]
    run(args + [os.path.join(ROOT, "upload.py")])

    exe = os.path.join(DIST, NAME + (".exe" if os.name == "nt" else ""))
    # smoke test: it starts, and the bundled link code and crypto load (--pair makes a key)
    run([exe, "--help"])
    run([exe, "--pair", "--link-config", os.path.join(WORK, "smoke", "link.json")])
    tag = "%s-%s" % (platform.system().lower(), platform.machine().lower().replace("amd64", "x86_64"))
    named = os.path.join(DIST, "%s-%s%s" % (NAME, tag, ".exe" if os.name == "nt" else ""))
    shutil.copy2(exe, named)
    print("built %s (%.1f MB)" % (named, os.path.getsize(named) / 1e6))
    return 0


if __name__ == "__main__":
    sys.exit(main())