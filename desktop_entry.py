import sys


def main():
    if len(sys.argv) > 1:
        from primal.cli import main as cli_main

        return cli_main(sys.argv[1:])
    from primal.desktop import main as desktop_main

    return desktop_main()


if __name__ == "__main__":
    raise SystemExit(main())
