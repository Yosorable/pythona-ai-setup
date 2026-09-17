"""Run the local model settings in Pythona, or preview the page in a desktop browser."""


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", action="store_true", help="serve a browser preview with simulated model responses")
    parser.add_argument("--port", type=int, default=8879, help="loopback port for the browser preview")
    parser.add_argument("--language", default="en", help="language for the browser preview")
    args = parser.parse_args()
    if args.preview:
        from ai_setup.preview import serve
        serve(args.port, args.language)
    else:
        from ai_setup.ui import main as open_settings
        open_settings()


if __name__ == "__main__":
    main()
