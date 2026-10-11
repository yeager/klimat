"""Command-line entry point for the packaged climate adapter sidecar."""

import asyncio
import json
import sys

from adapters import handle


def main() -> int:
    try:
        request = json.load(sys.stdin)
        response = asyncio.run(handle(request))
        json.dump(response, sys.stdout)
        sys.stdout.write("\n")
        return 0
    except Exception as error:
        print(str(error) or error.__class__.__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
