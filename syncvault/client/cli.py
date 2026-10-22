import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import httpx
import typer

from syncvault.client.hashing import hash_file
from syncvault.client.pending import cancel_pending, export_snapshot, retry_pending
from syncvault.client.planner import plan as make_plan
from syncvault.client.restore import restore as restore_file
from syncvault.client.restore import verify_remote_content
from syncvault.client.scanner import scan
from syncvault.client.state import State
from syncvault.client.transfer import push as push_files
from syncvault.client.transfer import PushFailed
from syncvault.client.transport import Remote
from syncvault.shared.paths import logical_path as validate_path

app = typer.Typer(no_args_is_help=True, help="SyncVault：单向、保留历史的文件备份")
pending_app = typer.Typer(no_args_is_help=True, help="保留、导出和显式重试上传任务")
app.add_typer(pending_app, name="pending")
RootOption = Annotated[Path | None, typer.Option(help="已初始化的备份根目录，默认从当前目录查找")]


def emit(value):
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


@contextmanager
def errors():
    try:
        yield
    except typer.Exit:
        raise
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
    continue_on_error: bool = False,
):
    """先续传未完成快照，再上传新增或修改文件。"""
    with errors(), State(root) as state, remote_for(state, concurrency) as remote:
        try:
            versions = push_files(state, remote, concurrency, chunk_size, full, progress=emit,
                                  continue_on_error=continue_on_error)
        except PushFailed as error:
            emit({"committed": len(error.versions), "versions": error.versions,
                  "failures": error.failures})
            raise typer.Exit(1) from error
        emit({"committed": len(versions), "versions": versions})


@app.command()
def status(root: Path):
    """查看本地已备份清单和可恢复的上传会话。"""
    with errors(), State(root) as state:
        emit({"root": str(state.root), "manifest": state.manifest(), "pending": state.pending()})


@pending_app.command("list")
def pending_list(root: RootOption = None):
    """查看待处理任务与重试前保留的归档任务。"""
    with errors(), State.discover(root) as state:
        emit({"pending": state.pending(), "archived": state.archived()})


@pending_app.command("export")
def pending_export(logical_path: str, output: Annotated[Path, typer.Option()],
                   root: RootOption = None, archive: str | None = None):
    """校验并导出快照，默认不覆盖；--archive 指定归档任务 ID。"""
    with errors(), State.discover(root) as state:
        emit(export_snapshot(state, logical_path, output, archive))


@pending_app.command("cancel")
def pending_cancel(logical_path: str, root: RootOption = None):
    """取消远端 OPEN 会话并暂停本地任务，保留快照；已完成的会话更新本地清单。"""
    with errors(), State.discover(root) as state, remote_for(state) as remote:
        emit(cancel_pending(state, remote, validate_path(logical_path)))


@pending_app.command("retry")
def pending_retry(logical_path: str, root: RootOption = None,
                  base_version: str | None = None, from_source: bool = False):
    """重新排队，随后运行 push；--base-version 显式选择基准，--from-source 重新取快照。"""
    with errors(), State.discover(root) as state, remote_for(state) as remote:
        emit(retry_pending(state, remote, validate_path(logical_path), base_version, from_source))


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
def verify(root: Path, full: bool = False, remote_content: bool = False):
    """核对清单；--full 重算本地哈希，--remote-content 下载并校验命名空间当前版本。"""
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
        remote_checked = 0
        if remote_content:
            for path, file in files.items():
                try:
                    verify_remote_content(remote, file["current_version"])
                    remote_checked += 1
                except (ValueError, RuntimeError, httpx.HTTPError) as error:
                    problems.append({"path": path, "reason": f"远端内容检查失败: {error}"})
        emit({"mode": "full" if full else "quick", "problems": problems,
              "local_missing_remote_retained": sorted(set(manifest) - present),
              "remote_content_checked": remote_checked,
              "note": ("已下载校验命名空间当前版本；不含历史版本" if remote_content
                       else "仅检查远端元数据；使用 --remote-content 检查实际内容")})
        if problems:
            raise typer.Exit(1)


if __name__ == "__main__":
    app()
