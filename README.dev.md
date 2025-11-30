# LLAMATOR development instructions

## Install pre-commit

To ensure code quality we use pre-commit hook with several checks. Setup it by:

```bash
pre-commit install
```

All updated files will be reformatted and linted before the commit.

Reformat and lint all files in the project:

```bash
pre-commit run --all-files
```

The used linters are configured in `.pre-commit-config.yaml`. You can use `pre-commit autoupdate` to bump tools to the
latest versions.