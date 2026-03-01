"""Entry point for running the bridge server: python -m axi.bridge <socket_path>

Delegates to procmux.server.main().
"""

from procmux.server import main

if __name__ == "__main__":
    main()
