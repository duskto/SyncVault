import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    database_url: str = "sqlite:///.syncvault-server/metadata.db"
    storage_root: Path = Path(".syncvault-server/data")
    tokens: dict[str, str] = field(default_factory=dict)
    session_ttl: int = 86400
    max_file_size: int = 100 * 1024**3
    max_chunks: int = 100_000
    auto_create: bool = False

    @classmethod
    def from_env(cls):
        tokens = json.loads(os.getenv("SYNCVAULT_TOKENS", "{}"))
        if not isinstance(tokens, dict) or any(
            not isinstance(k, str) or not k or not isinstance(v, str) or not v
            for k, v in tokens.items()
        ):
            raise ValueError("SYNCVAULT_TOKENS must map nonempty bearer tokens to owner IDs")
        return cls(
            database_url=os.getenv("SYNCVAULT_DATABASE_URL", cls.database_url),
            storage_root=Path(os.getenv("SYNCVAULT_STORAGE_ROOT", str(cls.storage_root))),
            tokens=tokens,
            session_ttl=int(os.getenv("SYNCVAULT_SESSION_TTL", "86400")),
            max_file_size=int(os.getenv("SYNCVAULT_MAX_FILE_SIZE", str(100 * 1024**3))),
            max_chunks=int(os.getenv("SYNCVAULT_MAX_CHUNKS", "100000")),
            auto_create=os.getenv("SYNCVAULT_AUTO_CREATE", "0") == "1",
        )
