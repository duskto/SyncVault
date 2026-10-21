import hashlib
import os
import tempfile
from pathlib import Path

import httpx

from syncvault.client.transport import APIError, retry_delay


def restore(remote, version, output: Path, overwrite=False):
    output = output.absolute()
    if output.exists() or output.is_symlink():
        if not overwrite:
            raise FileExistsError(f"目标已存在，请显式传入 --overwrite: {output}")
        if output.is_dir():
            raise IsADirectoryError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(6):
        descriptor, name = tempfile.mkstemp(prefix=".syncvault-restore-", dir=output.parent)
        temporary = Path(name)
        response = None
        try:
            digest = hashlib.sha256()
            size = 0
            with os.fdopen(descriptor, "wb") as target:
                with remote.client.stream(
                    "GET", f"/api/v1/versions/{version['version_id']}/content",
                    headers={"Accept-Encoding": "identity"},
                ) as response:
                    if response.status_code != 200:
                        response.read()
                        raise APIError(response)
                    for data in response.iter_bytes(1024 * 1024):
                        size += len(data)
                        if size > version["size_bytes"]:
                            raise ValueError("下载长度超出预期")
                        target.write(data)
                        digest.update(data)
                    target.flush()
                    os.fsync(target.fileno())
            if digest.hexdigest() != version["sha256"] or size != version["size_bytes"]:
                raise ValueError("下载完整性校验失败，原目标未改动")
            if overwrite:
                os.replace(temporary, output)
            else:
                # link provides atomic no-clobber publication, including concurrent creators.
                os.link(temporary, output)
                temporary.unlink()
            directory = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return output
        except (httpx.TransportError, APIError) as error:
            if isinstance(error, APIError) and error.status not in (408, 429, 500, 502, 503, 504):
                raise
            if attempt == 5:
                raise
            remote.sleep(retry_delay(response, attempt))
        finally:
            temporary.unlink(missing_ok=True)
    raise AssertionError("unreachable")
