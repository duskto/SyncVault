"""Generate synthetic input only, without reading personal files."""

import argparse
import random
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--large-mib", type=int, default=64)
    args = parser.parse_args()
    if args.large_mib < 0:
        parser.error("large-mib must be nonnegative")
    args.directory.mkdir(parents=True, exist_ok=True)
    randomizer = random.Random(42)
    samples = {"empty.bin": b"", "one.bin": b"1", "hello.txt": "SyncVault 示例\n".encode()}
    for name, content in samples.items():
        with (args.directory / name).open("xb") as output:
            output.write(content)
    with (args.directory / "large.bin").open("xb") as output:
        for _ in range(args.large_mib):
            output.write(randomizer.randbytes(1024 * 1024))
    print(args.directory.resolve())


if __name__ == "__main__":
    main()
