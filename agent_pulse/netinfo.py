"""Reachable-URL detection and QR generation, shared by the CLI
(`setup-phone`) and the collector (`/api/connect-info`, `/api/qr`)."""
import time
import socket
import subprocess
from typing import Optional, List, Dict

# Network addresses barely change; cache probes so a slow/wedged `tailscale`
# only costs the first caller per minute, not every Connect-phone request.
_CACHE_TTL = 60
_cache: Dict[str, tuple] = {}


def _cached(key: str, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[1] < _CACHE_TTL:
        return hit[0]
    val = fn()
    _cache[key] = (val, time.time())
    return val


def lan_ip() -> Optional[str]:
    """Best-effort primary LAN IP (the interface that routes outward).
    Doesn't actually send anything."""
    def probe():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
            finally:
                s.close()
        except Exception:
            return None
    return _cached("lan", probe)


def tailscale_ip() -> Optional[str]:
    def probe():
        try:
            out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                                 text=True, timeout=1.5)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip().splitlines()[0].strip()
        except Exception:
            pass
        return None
    return _cached("tailscale", probe)


def _url(host: str, port: int, token: Optional[str]) -> str:
    suffix = f"/?token={token}" if token else "/"
    return f"http://{host}:{port}{suffix}"


def reachable_urls(port: int, token: Optional[str]) -> List[Dict[str, str]]:
    """URLs a phone could use to reach this collector, best first."""
    urls = []
    ip = lan_ip()
    if ip:
        urls.append({"label": "Same Wi-Fi", "url": _url(ip, port, token)})
    ts = tailscale_ip()
    if ts:
        urls.append({"label": "Tailscale (any network)", "url": _url(ts, port, token)})
    return urls


def qr_svg(data: str) -> Optional[str]:
    """QR code as a standalone SVG string (no Pillow needed). None if the
    qrcode library isn't installed."""
    try:
        import qrcode
        import qrcode.image.svg
        img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage,
                          box_size=12, border=2)
        from io import BytesIO
        buf = BytesIO()
        img.save(buf)
        return buf.getvalue().decode("utf-8")
    except Exception:
        return None


def qr_terminal(data: str) -> Optional[str]:
    """QR rendered with unicode half-blocks for a terminal. None if qrcode
    isn't installed."""
    try:
        import qrcode
        qr = qrcode.QRCode(border=2)
        qr.add_data(data)
        qr.make(fit=True)
        m = qr.get_matrix()
        # Two rows per line using half-block chars keeps it compact + square.
        lines = []
        for y in range(0, len(m), 2):
            row = ""
            for x in range(len(m[y])):
                top = m[y][x]
                bot = m[y + 1][x] if y + 1 < len(m) else False
                row += "█" if top and bot else "▀" if top else "▄" if bot else " "
            lines.append(row)
        return "\n".join(lines)
    except Exception:
        return None
