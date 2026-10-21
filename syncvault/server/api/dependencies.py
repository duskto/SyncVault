import secrets

from fastapi import Header, Request

from syncvault.shared.protocol import ProtocolError


def owner(request: Request, authorization: str = Header(default="")) -> str:
    if authorization.startswith("Bearer "):
        token = authorization[7:]
        for candidate, user in request.app.state.settings.tokens.items():
            if secrets.compare_digest(token.encode(), candidate.encode()):
                return user
    raise ProtocolError(401, "UNAUTHORIZED", "需要有效的 Bearer token")
