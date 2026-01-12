# Narou-dl

Download a novel from Syosetu and build an EPUB for personal reading (CLI script).
Respect the site's Terms of Service and robots.txt. Keep delays low-load.

## Setup (uv)

```bash
uv venv
uv sync
```

## Usage

```bash
uv run narou-dl https://ncode.syosetu.com/n1234ab/ -o ./out --delay 1.2
uv run narou-dl n1234ab --vertical --from-ep 1 --to-ep 50
```

## Options

- `--delay` : delay between requests (seconds, default 1.0)
- `--retry` : retry count on network errors
- `--timeout` : HTTP timeout (seconds)
- `--from-ep` / `--to-ep` : episode range
- `--no-preface` / `--no-afterword` : omit those sections
- `--vertical` : apply vertical writing mode CSS
- `--user-agent` : override User-Agent string

## Notes

- The script uses the Narou API for episode count when possible, and the index page
  for the actual episode URLs.
- Short stories are handled by treating the top page as the single episode.
