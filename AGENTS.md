# AGENTS.md

- [SPEC.md](./SPEC.md): specifications of the project
- When the user asks for a change, always check that it matches the specifications, otherwise ask for permission to update the specifications first.
- When the user asks for a change, try to write a test for it first, then implement the change, then run the test to validate.
- After modifying or creating code, run `just fix` (formats the code and auto-fixes lint issues).
- At the end of a code change, run `just check` (format check + lint + type check) to validate before finishing.
- The ruff formatter is authoritative: don't "fix" its output. In particular, `except A, B:` (unparenthesized) is valid Python 3.14 (PEP 758) and is ruff's canonical form — leave it as-is.
