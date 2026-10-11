"""Collect runtime dependency license texts into the desktop resource bundle."""

from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement


ROOTS = ("python-melcloud", "midea-local", "aiohttp", "aiomelcloudhome", "thinqconnect")
LICENSE_NAMES = ("license", "copying", "notice", "copyright")
OUTPUT = Path(__file__).parent / "dist" / "THIRD_PARTY_LICENSES.txt"
GREE_LICENSE = Path(__file__).parent.parent / "licenses" / "gree-climate-MIT.txt"


def main() -> None:
    pending = list(ROOTS)
    collected: dict[str, metadata.Distribution] = {}
    while pending:
        name = pending.pop()
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        key = distribution.metadata.get("Name", name).lower().replace("_", "-")
        if key in collected:
            continue
        collected[key] = distribution
        for requirement_text in distribution.requires or ():
            requirement = Requirement(requirement_text)
            if requirement.marker and not requirement.marker.evaluate():
                continue
            pending.append(requirement.name)

    sections = [
        "Third-party software included with Klimat",
        "===========================================",
        "",
        "Klimat includes the following Python libraries and their dependencies.",
        "Their license terms and notices are reproduced below.",
    ]
    for name, distribution in sorted(collected.items()):
        files = []
        for entry in distribution.files or ():
            basename = Path(str(entry)).name.lower()
            if any(basename == key or basename.startswith(f"{key}.") for key in LICENSE_NAMES):
                path = distribution.locate_file(entry)
                try:
                    content = path.read_text(encoding="utf-8").strip()
                except (OSError, UnicodeError):
                    continue
                if content:
                    files.append((str(entry), content))
        title = f"{distribution.metadata.get('Name', name)} {distribution.version}"
        sections.extend(["", "-" * len(title), title, "-" * len(title)])
        if files:
            for relative_path, content in sorted(files):
                sections.extend([f"\n[{relative_path}]", content])
        else:
            license_name = distribution.metadata.get("License-Expression") or distribution.metadata.get("License") or "License not stated in package metadata"
            sections.append(f"License metadata: {license_name}")

    if GREE_LICENSE.is_file():
        sections.extend(["", "gree-climate 0.1.0", "------------------", GREE_LICENSE.read_text(encoding="utf-8").strip()])

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text("\n".join(sections).rstrip() + "\n", encoding="utf-8")
    print(f"Wrote license notices for {len(collected)} runtime packages to {OUTPUT}")


if __name__ == "__main__":
    main()
