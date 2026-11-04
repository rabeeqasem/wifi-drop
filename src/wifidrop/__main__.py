import sys


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        from .cli import main as cli_main
        return cli_main(sys.argv[2:])
    from .gui import main as gui_main
    return gui_main(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
