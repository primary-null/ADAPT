"""Build the auditable on-device helper using local javac and Android D8 only."""
import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--build-tools", default="36.0.0")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    resource = root / "src/adapt_adb/resources/dex_bridge"
    source = resource / "AdaptDexBridge.java"
    d8jar = args.sdk / "build-tools" / args.build_tools / "lib/d8.jar"
    android = args.sdk / "platforms/android-33/android.jar"
    java, javac = shutil.which("java"), shutil.which("javac")
    if not java or not javac or not d8jar.is_file() or not android.is_file():
        raise SystemExit("Local Java and Android 33/build-tools D8 are required; no downloads performed.")
    with tempfile.TemporaryDirectory(prefix="adapt-dex-build-") as temporary:
        classes = Path(temporary) / "classes"
        classes.mkdir()
        subprocess.run([javac, "--release", "8", "-d", str(classes), str(source)], check=True)
        output = Path(temporary) / "adapt-dex-bridge.jar"
        subprocess.run([java, "-cp", str(d8jar), "com.android.tools.r8.D8", "--min-api", "29", "--lib", str(android),
                        "--output", str(output), *map(str, classes.rglob("*.class"))], check=True)
        data = output.read_bytes()
        (resource / output.name).write_bytes(data)
    receipt = {"schema_version": 1, "min_api": 29, "build_tools": args.build_tools,
               "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
               "jar_sha256": hashlib.sha256(data).hexdigest()}
    (resource / "build.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
