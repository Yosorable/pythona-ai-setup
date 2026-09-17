"""Run the integration test in a sibling Pythona checkout without keeping files there."""

import argparse
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pythona", type=Path, help="path to the Pythona app checkout")
    parser.add_argument("--device", required=True, help="arm64 iOS simulator UDID")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    app = args.pythona.resolve()
    source = root / "tests/ios/LocalLLMIntegrationTests.swift"
    link = app / "PythonaTests/LocalLLMIntegrationTests.swift"
    if link.exists() or link.is_symlink():
        raise SystemExit(f"Refusing to replace an existing test: {link}")
    link.symlink_to(source)
    try:
        subprocess.run([
            "xcodebuild", "-project", "Pythona.xcodeproj", "-scheme", "Pythona", "-configuration", "Debug",
            "-destination", f"platform=iOS Simulator,id={args.device},arch=arm64",
            "-only-testing:PythonaTests/LocalLLMIntegrationTests", "-parallel-testing-enabled", "NO", "test",
        ], cwd=app, check=True)
    finally:
        if link.is_symlink() and link.resolve() == source:
            link.unlink()


if __name__ == "__main__":
    main()
