"""CLI entry point: python -m discordquery <subcommand> [args]

Subcommands:
    query   — Query Discord message history (guilds, channels, history, search)
    wait    — Wait for new messages in a channel (polling)
"""

import sys


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print("Usage: python -m discordquery <subcommand> [args]")
        print()
        print("Subcommands:")
        print("  query   Query Discord message history (guilds, channels, history, search)")
        print("  wait    Wait for new messages in a channel")
        print()
        print("Run 'python -m discordquery <subcommand> --help' for details.")
        sys.exit(0)

    subcommand = sys.argv[1]
    argv = sys.argv[2:]

    if subcommand == "query":
        from discordquery.query import main as query_main

        query_main(argv)
    elif subcommand == "wait":
        from discordquery.wait import main as wait_main

        wait_main(argv)
    else:
        print(f"Unknown subcommand: {subcommand}", file=sys.stderr)
        print("Available: query, wait", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
