## Workflow

Always run ruff and pytest and ty and pyrefly (with uv) after making any changes

You may refer to the papers in @references/ for implementation guidance

For judging output quality (renders, strategy comparison, instruction
sensibility, corpus regression sweeps), follow
docs/self-evaluation-playbook.md and the compare-strategies /
inspect-instructions / eval-corpus skills in .claude/skills/

## Development Notes

- The project supports Python 3.12+
- Uses uv for dependency management instead of traditional pip/setuptools
- Code style uses Ruff formatter and linter

## Python Practices
- Always use or add type hints
- Prefer @dataclasses where applicable
- Always use f-string over string formatting or concatentation (except in logging strings)
- Use async generators and comprehensions when they might provide benefits
- Use underscores in large numeric literals
- Use walrus assignment := where applicable
- Prefer to use named arguments when calling a method with more than one argument
- Use "list" instead of "List" and "dict" instead of "Dict" and "|" instead of "Union" for types
- Use "Self" for applicable types
- Use Structural Pattern Matching (match...case) where applicable
- Always use pathlib.Path for file operations, never use os.path
