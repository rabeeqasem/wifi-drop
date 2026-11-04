"""Terminal version: wifi-drop --cli FILE [...]"""
import argparse
import sys

from .server import DEFAULT_PORT, DropServer, human


def print_qr(url):
    try:
        import qrcode
    except ImportError:
        print("(install the 'qrcode' Python package to show a QR code)")
        return
    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="wifi-drop --cli",
                                 description="Send files from this PC to an iPhone over Wi-Fi.")
    ap.add_argument("paths", nargs="*", help="files or folders to share")
    ap.add_argument("-p", "--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("-r", "--receive-dir", help="where files uploaded from the iPhone are saved")
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)

    def on_event(kind, name, detail):
        if kind == "download":
            print(f"  ↓ iPhone is downloading {name}")
        elif kind == "zip":
            print(f"  ↓ iPhone is downloading {name} as ZIP")
        elif kind == "received":
            print(f"  ↑ received {name} → {detail}")

    server = DropServer(receive_dir=args.receive_dir, port=args.port, on_event=on_event)
    for p in args.paths:
        try:
            server.files.add(p)
        except FileNotFoundError:
            sys.exit(f"wifi-drop: not found: {p}")

    snap = server.files.snapshot()
    if snap:
        print(f"\nSharing {len(snap)} file(s), {human(sum(s for *_, s in snap))}:")
        for _fid, name, _p, _s in snap[:10]:
            print(f"  • {name}")
        if len(snap) > 10:
            print(f"  … and {len(snap) - 10} more")
    else:
        print("\nReceive-only mode (nothing to send).")
    url = server.url()
    print("\nScan with your iPhone camera (same Wi-Fi network):\n")
    print_qr(url)
    print(f"  {url}\n")
    print(f"Files from the iPhone are saved to: {server.receive_dir}")
    print("Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.httpd.server_close()
