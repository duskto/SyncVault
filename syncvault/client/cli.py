import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import httpx
import typer

from syncvault.client.hashing import hash_file
from syncvault.client.planner import plan as make_plan
from syncvault.client.restore import restore as restore_file
from syncvault.client.scanner import scan
from syncvault.client.state import State
from syncvault.client.transfer import push as push_files
from syncvault.client.transport import Remote
from syncvault.shared.paths import logical_path as validate_path

app = typer.Typer(no_args_is_help=True, help="SyncVault：单向、保留历史的文件备份")
RootOption = Annotated[Path | None, typer.Option(help="已初始化的备份根目录，默认从当前目录查找")]


def emit(value):
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


@contextmanager
def errors():
    try:
        yield
    except (ValueError, OSError, RuntimeError, sqlite3.Error, httpx.HTTPError) as error:
        typer.echo(f"错误: {error}", err=True)
        raise typer.Exit(1) from error


def remote_for(state, concurrency=4):
    return Remote(state.config["server"], os.getenv("SYNCVAULT_TOKEN"), concurrency)


@app.command()
def init(root: Path, server: str = typer.Option(...), namespace: str = "personal"):
    """初始化配置和 SQLite 状态；令牌只从环境变量读取。"""
    with errors(), State.initialize(root, server, namespace) as state:
        with remote_for(state) as remote:
            remote.request("GET", "/health")
            remote.request("GET", "/api/v1/files", params={"namespace": namespace, "limit": 1})
        emit({"root": str(state.root), **state.config, "health": "ok"})


@app.command()
def plan(root: Path, full: bool = False):
    """预览新增、修改与跳过的文件；--full 重新计算所有普通文件的哈希。"""
    with errors(), State(root) as state:
        emit([operation.as_dict() for operation in make_plan(state, full)])


@app.command()
def push(
    root: Path,
    concurrency: Annotated[int, typer.Option(min=1, max=32)] = 4,
    chunk_size: Annotated[int, typer.Option(min=1, max=64 * 1024 * 1024)] = 4 * 1024 * 1024,
    full: bool = False,
):
    """先续传未完成快照，再上传新增或修改文件。"""
    with errors(), State(root) as state, remote_for(state, concurrency) as remote:
        versions = push_files(state, remote, concurrency, chunk_size, full, progress=emit)
        emit({"committed": len(versions), "versions": versions})


@app.command()
def status(root: Path):
    """查看本地已备份清单和可恢复的上传会话。"""
    with errors(), State(root) as state:
        emit({"root": str(state.root), "manifest": state.manifest(), "pending": state.pending()})


@app.command()
def versions(logical_path: str, root: RootOption = None):
    """列出指定逻辑路径的所有历史版本。"""
    with errors(), State.discover(root) as state, remote_for(state) as remote:
        file = remote.find_file(state.config["namespace"], validate_path(logical_path))
        emit(remote.list_all(f"/api/v1/files/{file['file_id']}/versions"))


@app.command()
def restore(
    logical_path: str, version: Annotated[str, typer.Option()],
    output: Annotated[Path, typer.Option()], root: RootOption = None,
    overwrite: bool = False,
):
    """恢复一个不可变版本；完整校验后原子发布，默认不覆盖已有文件。"""
    with errors(), State.discover(root) as state, remote_for(state) as remote:
        file = remote.find_file(state.config["namespace"], validate_path(logical_path))
        history = remote.list_all(f"/api/v1/files/{file['file_id']}/versions")
        chosen = next((item for item in history if item["version_id"] == version), None)
        if chosen is None:
            raise ValueError("指定版本不属于此文件")
        result = restore_file(remote, chosen, output, overwrite)
        emit({"output": str(result), "sha256": chosen["sha256"]})


@app.command()
def verify(root: Path, full: bool = False):
    """核对本地清单与远端当前版本；--full 对本地内容重新计算 SHA-256。"""
    with errors(), State(root) as state, remote_for(state) as remote:
        manifest = state.manifest()
        files = {item["logical_path"]: item for item in remote.list_all(
            "/api/v1/files", namespace=state.config["namespace"]
        )}
        problems = []
        present = set()
        for entry in scan(state.root):
            if entry.skip_reason:
                continue
            present.add(entry.logical_path)
            saved = manifest.get(entry.logical_path)
            if saved is None:
                problems.append({"path": entry.logical_path, "reason": "尚未备份"})
                continue
            if full:
                matches = hash_file(state.root / entry.logical_path) == saved["sha256"]
            else:
                matches = (
                    saved["size_bytes"] == entry.size_bytes and saved["mtime_ns"] == entry.mtime_ns
                )
            if not matches:
                problems.append({"path": entry.logical_path, "reason": "本地内容已变化"})
        for path, saved in manifest.items():
            remote_file = files.get(path)
            if (
                remote_file is None
                or remote_file["current_version_id"] != saved["last_synced_version"]
                or remote_file["current_version"]["sha256"] != saved["sha256"]
                or remote_file["current_version"]["size_bytes"] != saved["size_bytes"]
            ):
                problems.append({"path": path, "reason": "远端版本与本地清单不一致"})
        emit({"mode": "full" if full else "quick", "problems": problems,
              "local_missing_remote_retained": sorted(set(manifest) - present),
              "note": "检查远端元数据；不下载全部远端内容，不是服务端磁盘巡检"})
        if problems:
            raise typer.Exit(1)


if __name__ == "__main__":
    app()
