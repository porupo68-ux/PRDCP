"""Small executable entry point; operator behavior lives in cli_app/."""

from cli_app import entrypoint


if __name__ == "__main__":
    raise SystemExit(entrypoint())
