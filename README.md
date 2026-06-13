# Overleaf-Git

Unofficial tool for cloning Overleaf project as a Git repository.

## Setup

1. Sign up to [Overleaf](https://overleaf.com)
2. Copy your browser cookies into a file (default location: `~/.overleaf.cookies`)

## Usage

Clone an Overleaf project:

```shell
python3 overleaf_git.py clone <overleaf-project-id>
```

This is equivalent to:

```shell
python3 overleaf_git.py clone <overleaf-project-id> --cookies ~/.overleaf.cookies
```
