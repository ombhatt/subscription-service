"""Write the API's OpenAPI schema where the web app generates its types from.

    python -m scripts.export_openapi            # write web/lib/openapi.json
    python -m scripts.export_openapi --check    # exit 1 if it is stale

`make api-types` runs this and then openapi-typescript, which turns the schema
into web/lib/openapi.gen.ts. Both files are committed, and CI checks each
against its source: the schema against the app (here, with --check), the
TypeScript against the schema (in the web job). A response model changed
without regenerating fails CI rather than drifting from the frontend.

The schema is built from the app object, not fetched from /openapi.json,
which production does not serve.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

TARGET = pathlib.Path(__file__).parents[1] / "web" / "lib" / "openapi.json"


def render() -> str:
    from app.main import app

    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if the file is stale")
    args = parser.parse_args()

    current = render()
    if args.check:
        if not TARGET.exists() or TARGET.read_text() != current:
            print(f"{TARGET} is stale; run `make api-types` and commit the result", file=sys.stderr)
            return 1
        print(f"{TARGET.name} matches the app")
        return 0
    TARGET.write_text(current)
    print(f"wrote {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
