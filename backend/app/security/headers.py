from __future__ import annotations

from urllib.parse import urlsplit


def browser_security_headers(authority: str = "") -> dict[str, str]:
    authority_origin = trusted_origin(authority)
    connect_sources = ["'self'"]
    frame_sources = ["'none'"]
    form_sources = ["'self'"]
    if authority_origin:
        connect_sources.append(authority_origin)
        frame_sources = [authority_origin]
        form_sources.append(authority_origin)
    policy = "; ".join(
        (
            "default-src 'self'",
            "base-uri 'none'",
            f"connect-src {' '.join(connect_sources)}",
            f"form-action {' '.join(form_sources)}",
            f"frame-src {' '.join(frame_sources)}",
            "frame-ancestors 'self'",
            "img-src 'self' data: blob:",
            "object-src 'none'",
            "script-src 'self'",
            "style-src 'self' 'unsafe-inline'",
            "worker-src 'self' blob:",
        )
    )
    return {
        "Content-Security-Policy": policy,
        "Cross-Origin-Opener-Policy": "same-origin",
        "Permissions-Policy": "camera=(), geolocation=(), microphone=()",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "SAMEORIGIN",
    }


def trusted_origin(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


__all__ = ["browser_security_headers", "trusted_origin"]
