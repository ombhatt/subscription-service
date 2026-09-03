import sys

# Fail here with an explanation rather than deep inside Pydantic's annotation
# evaluation, which reports `unsupported operand type(s) for |` and says nothing
# about the interpreter. 3.11 is the floor: PEP 604 unions are evaluated at
# runtime by pydantic-settings, and the code uses `datetime.UTC`.
if sys.version_info < (3, 11):
    raise RuntimeError(
        f"This service requires Python 3.11+, but is running {sys.version.split()[0]} "
        f"from {sys.executable}.\n"
        "Recreate the virtualenv with a newer interpreter, for example:\n"
        "    rm -rf .venv && python3.11 -m venv .venv && "
        ".venv/bin/pip install -r requirements-dev.txt"
    )
