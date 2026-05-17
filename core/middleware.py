class SecurityHeadersMiddleware:
    """Attach security-relevant HTTP response headers to every response."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.setdefault('X-Content-Type-Options', 'nosniff')
        response.setdefault('X-DNS-Prefetch-Control', 'off')
        response.setdefault('X-Download-Options', 'noopen')
        response.setdefault('X-Permitted-Cross-Domain-Policies', 'none')
        response.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
        response.setdefault(
            'Permissions-Policy',
            'camera=(), microphone=(), geolocation=(), payment=()',
        )
        # Content-Security-Policy — permits CDN resources actually used by the app.
        # 'unsafe-inline' is required because the templates embed <script> and <style> blocks.
        response.setdefault(
            'Content-Security-Policy',
            (
                "default-src 'self'; "
                "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
                "style-src 'self' https://cdn.jsdelivr.net https://fonts.googleapis.com 'unsafe-inline'; "
                "font-src 'self' https://cdn.jsdelivr.net https://fonts.gstatic.com; "
                "img-src 'self' data: https:; "
                "connect-src 'self'; "
                "frame-ancestors 'none';"
            ),
        )
        return response