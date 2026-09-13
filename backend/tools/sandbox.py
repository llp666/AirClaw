"""Docker 沙箱执行器。

terminal / python_repl / code_test 技能的一切代码执行动作，统一经由本模块进入
无网络容器。隔离参数在此处**硬编码固化**，不接受 Agent 输入覆盖。

对应 PRD 第八章「Docker 沙箱执行设计」：
    --network=none    无网络模式，杜绝借道容器发起任何外联
    --read-only       根文件系统只读，仅 /tmp 与输出目录可写
    --user 1000:1000  非 root 运行
    不注入宿主机密钥、凭证与环境变量
    遮蔽审计日志与凭证文件（见 SANDBOX_MASKED_DIRS / SANDBOX_MASKED_FILES）
    每次执行创建独立容器，结束即销毁（--rm 语义）
"""

from __future__ import annotations

import logging
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import docker
from docker.errors import DockerException, ImageNotFound
from docker.types import Mount

from config import Settings

logger = logging.getLogger("airclaw.sandbox")

# 输出截断上限（字符）
OUTPUT_LIMIT = 5000

SANDBOX_SRC = "/workspace/src"
SANDBOX_OUT = "/workspace/out"
SANDBOX_WORKDIR = "/workspace"

#: 沙箱内要遮蔽的目录（相对项目根目录）。
#:
#: 「挂载范围」即权限范围：项目根目录整体只读挂进沙箱，里面的每一样东西 Agent 都读得到，
#: 而沙箱是由**用户提问**驱动的，等价于「普通用户可达」。按「Agent 用正常工具链拿不到的
#: 东西，也不该能从沙箱里读」这条线：
#:
#:   backend/audit     审计日志。PRD 第八章第 4 条：普通用户不可通过前端或文件接口访问；
#:                     沙箱是第三条路，之前没堵上。
#:   backend/sessions  会话文件。当前会话的内容已由 System Prompt 带入，其余会话属于
#:                     跨会话数据，没有工具会去读它。
#:   backend/rules     审查规则库。让被审查方读到检测规则，等于把绕过的方法一并交出去。
#:                     遮的是整个目录（含 check_rules.py）——只遮 JSON 文件的话，
#:                     命名卷模式下遮不住：那种模式的卷由镜像播种，而镜像并未排除
#:                     backend/rules/，文件遮蔽又只在绑定模式生效。代价是沙箱里跑不了
#:                     规则回归，那是宿主机上的运维动作（见 AGENTS.md）。
#:   doc               需求与设计文档，既不入库也不入镜像（见 docker/Dockerfile.dockerignore），
#:                     且其中的 model.md 有明文第三方密钥。
#:
#: 用 tmpfs 遮蔽：挂的是**容器内路径**，与宿主机路径无关，绑定模式与命名卷模式通用，
#: 也不需要额外的宿主目录。被遮蔽处显示为一个空目录。
#:
#: 刻意用**可写**（rw）而不是只读：`import config` 在模块加载期就会 mkdir
#: `storage/tiktoken_cache`，`SessionManager` 实例化时会 mkdir `sessions/archive`——
#: 遮蔽成只读会让这些动作报 ReadOnlyFileSystem，把「跑一个测试」这类正常动作弄坏。
#: 可写 tmpfs 里的写入随容器销毁，既到不了宿主机，也不影响「被遮蔽内容不可见」这一点。
#: （正因如此，**storage/ 没有进这张表**：它在导入期就被 mkdir，遮蔽的风险大于收益。）
SANDBOX_MASKED_DIRS = ("backend/audit", "backend/sessions", "backend/rules", "doc")

#: 沙箱内要遮蔽的文件（相对项目根目录）：宿主机凭证不该躺在会被挂进沙箱的目录里。
#:
#: 文件遮蔽只在**绑定模式**下生效（tmpfs 盖不住单个文件，只能拿空文件 bind 覆盖；
#: 而命名卷模式下应用自身跑在容器里，`/srv/airclaw-src/...` 在宿主机上并不存在，
#: 从容器内发起 bind 挂载必然失败）。这里不影响安全：命名卷的卷由镜像播种，
#: 而镜像已排除 backend/.env。
SANDBOX_MASKED_FILES = ("backend/.env", "docker/.env")

#: 遮蔽文件用的空占位文件（放在沙箱临时区里，随用随建）。
#: 必须用「空文件 bind 覆盖」而不是 tmpfs —— Docker 不允许把 tmpfs 挂在单个文件上
#: （runc 报 "not a directory"）。
BLANK_NAME = "blank"


class SandboxError(RuntimeError):
    """沙箱不可用（Docker 未启动、镜像缺失等）。"""


@dataclass
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    mounts: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def render(self, limit: int = OUTPUT_LIMIT) -> str:
        """给 LLM 看的文本结果。输出超限时截断并标记。"""
        if self.timed_out:
            return f"[执行超时，容器已强制终止]\n{self._trunc(self.stdout, limit)}"

        parts = []
        if self.stdout:
            parts.append(self._trunc(self.stdout, limit))
        if self.stderr:
            parts.append(f"[stderr]\n{self._trunc(self.stderr, limit)}")
        if not parts:
            parts.append("(无输出)")
        body = "\n".join(parts)
        if self.exit_code != 0:
            body = f"[退出码 {self.exit_code}]\n{body}"
        return body

    @staticmethod
    def _trunc(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[:limit] + f"\n...[truncated，原始长度 {len(text)} 字符]"


class SandboxRunner:
    """一次性容器执行器。每次 run() 创建独立容器，无论成败都在 finally 中销毁。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: docker.DockerClient | None = None

    # ---- Docker 客户端 ----

    def _get_client(self) -> docker.DockerClient:
        if self._client is not None:
            return self._client
        try:
            # Docker Desktop 在 Windows 上的首次调用（建立 WSL2 文件系统共享）
            # 可能耗时数十秒，故放宽 API 超时，避免冷启动被误判为故障。
            client = docker.from_env(timeout=300)
            client.ping()
        except DockerException as exc:
            raise SandboxError(
                f"Docker 不可用，无法执行沙箱动作。请确认 Docker Desktop 已启动。原始错误：{exc}"
            ) from exc
        self._client = client
        return client

    def ensure_image(self) -> None:
        """镜像缺失时给出可操作的提示，而不是让 docker 抛原始错误。"""
        client = self._get_client()
        image = self._settings.sandbox_image
        try:
            client.images.get(image)
        except ImageNotFound as exc:
            raise SandboxError(
                f"沙箱镜像 {image} 不存在。构建命令（在仓库根目录执行）：\n"
                f"    docker build -f backend/sandbox/Dockerfile -t {image} ."
            ) from exc

    # ---- 执行 ----

    def run(
        self,
        argv: list[str],
        *,
        src_dir: Path | None = None,
        out_dir: Path | None = None,
        timeout: int | None = None,
        workdir: str = SANDBOX_WORKDIR,
        stdin_text: str | None = None,
    ) -> SandboxResult:
        """在无网络容器内执行 argv，返回结果。

        src_dir  被测代码目录，以只读挂载到 /workspace/src（默认项目根目录）
        out_dir  输出目录，以读写挂载到 /workspace/out（默认本次运行的临时目录）
        workdir  容器内工作目录，须为 /workspace 或 /workspace/out
                  （/workspace/src 是只读的，不能作为工作目录）
        """
        settings = self._settings
        client = self._get_client()
        self.ensure_image()

        src = Path(src_dir or settings.sandbox_src_path)
        owns_out = out_dir is None
        if owns_out:
            # 绑定模式下必须建在项目目录内，不能用系统临时目录：
            # Docker Desktop 只共享项目所在盘，挂载 %TEMP% 会触发目录共享确认而挂起。
            # 命名卷模式下 sandbox_tmp_path 指向输出卷内的路径，同样成立。
            out = Path(
                tempfile.mkdtemp(
                    prefix=f"run-{uuid.uuid4().hex[:8]}-",
                    dir=settings.sandbox_tmp_path,
                )
            )
        else:
            out = Path(out_dir)
            out.mkdir(parents=True, exist_ok=True)

        mounts = self._build_mounts(settings, src, out)
        mounts.extend(self._file_masks(settings, src))
        tmpfs = {"/tmp": "rw,size=512m", **self._dir_masks(src)}

        if settings.sandbox_uses_volume:
            # 沙箱容器以 uid 1000 运行，而应用容器通常以 root 运行，
            # tempfile.mkdtemp 建出的目录是 0700 root —— 沙箱连进都进不去，
            # 表现为「Permission denied」，且报错发生在被测脚本里，不易定位。
            # 这里的目录是单次执行的临时工作区，容器结束即销毁，放宽权限无副作用。
            try:
                os.chmod(out, 0o777)
            except OSError as exc:
                logger.warning("无法调整输出目录权限（%s）：%s", out, exc)

        container = None
        try:
            container = client.containers.create(
                image=settings.sandbox_image,
                command=argv,
                working_dir=workdir,
                # ---- 隔离参数：全部固化，Agent 无法覆盖 ----
                network_mode="none",
                mem_limit=settings.sandbox_memory,
                nano_cpus=int(float(settings.sandbox_cpus) * 1_000_000_000),
                pids_limit=settings.sandbox_pids_limit,
                read_only=True,
                tmpfs=tmpfs,
                user="1000:1000",
                mounts=mounts,
                # 不注入宿主机任何环境变量或凭证
                environment={},
                detach=True,
                auto_remove=False,
            )
            container.start()

            if stdin_text is not None:
                sock = container.attach_socket(params={"stdin": 1, "stream": 1})
                sock._sock.sendall(stdin_text.encode("utf-8"))
                sock._sock.close()

            timed_out = False
            try:
                status = container.wait(timeout=timeout or settings.sandbox_timeout)
                exit_code = int(status.get("StatusCode", -1))
            except Exception:
                # container.wait 超时由 requests 抛出，各种版本异常类型不一，统一按超时处理
                timed_out = True
                exit_code = -1
                try:
                    container.kill()
                except DockerException:
                    pass

            stdout = self._logs(container, stdout=True, stderr=False)
            stderr = self._logs(container, stdout=False, stderr=True)

            return SandboxResult(
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                timed_out=timed_out,
                mounts={"src": str(src), "out": str(out)},
            )
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except DockerException:
                    pass
            if owns_out:
                # 临时工作区随容器一并销毁
                import shutil

                shutil.rmtree(out, ignore_errors=True)

    # ---- 挂载构造 ----

    @staticmethod
    def _build_mounts(settings: Settings, src: Path, out: Path) -> list[Mount]:
        """构造沙箱容器的挂载项。

        两种模式的核心差别在于「源」是什么：

          绑定模式：源是本地路径，由 Docker daemon 在**宿主机**上解析。
                    因此容器内路径必须与宿主机路径一致，否则源找不到。
          命名卷模式：源是卷名，由 Docker 自己解析，与宿主机路径完全无关。
        """
        if settings.sandbox_uses_volume:
            return [
                Mount(
                    target=SANDBOX_SRC,
                    source=settings.sandbox_src_volume,
                    type="volume",
                    read_only=True,
                ),
                Mount(
                    target=SANDBOX_OUT,
                    source=settings.sandbox_out_volume,
                    type="volume",
                    read_only=False,
                ),
            ]
        return [
            Mount(
                target=SANDBOX_SRC,
                source=str(src.resolve()),
                type="bind",
                read_only=True,
            ),
            Mount(
                target=SANDBOX_OUT,
                source=str(out.resolve()),
                type="bind",
                read_only=False,
            ),
        ]

    @staticmethod
    def _dir_masks(src: Path) -> dict[str, str]:
        """要遮蔽的目录 → tmpfs 参数。

        可写（rw）：见 SANDBOX_MASKED_DIRS 的说明——只读会让导入期与被测代码里的
        mkdir 报 ReadOnlyFileSystem。写入落在 tmpfs 里，随容器销毁。

        **只遮蔽 src 里实际存在的目录**：容器 rootfs 是只读的，而 Docker 挂 tmpfs 前
        要先创建挂载点（mkdirat）。挂载树里没有那个路径时这一步必然失败，整个容器
        起不来——实测报
        `make mountpoint "/workspace/src/doc": ... read-only file system`。
        命名卷模式下交付镜像本就不含 doc/（dockerignore 排除），那里没有东西要遮，
        跳过即可；绑定模式下项目根目录整个挂进去，doc/ 存在，照常遮蔽。
        """
        return {
            f"{SANDBOX_SRC}/{rel}": "rw,size=1m"
            for rel in SANDBOX_MASKED_DIRS
            if (src / rel).is_dir()
        }

    @staticmethod
    def _file_masks(settings: Settings, src: Path) -> list[Mount]:
        """要遮蔽的文件 → 用空占位文件覆盖。

        只覆盖**确实存在**的：docker/.env 只在容器化部署时才有（compose 的 env_file）。
        """
        if settings.sandbox_uses_volume:
            return []
        present = [rel for rel in SANDBOX_MASKED_FILES if (src / rel).is_file()]
        if not present:
            return []

        blank = settings.sandbox_tmp_path / BLANK_NAME
        if not blank.is_file():
            blank.parent.mkdir(parents=True, exist_ok=True)
            blank.write_bytes(b"")
        return [
            Mount(
                target=f"{SANDBOX_SRC}/{rel}",
                source=str(blank),
                type="bind",
                read_only=True,
            )
            for rel in present
        ]

    def container_out_path(self, app_path: Path) -> str:
        """把应用侧的路径映射为沙箱容器内的路径。

        绑定模式下输出目录整体挂在 /workspace/out，故直接返回该路径。
        命名卷模式下卷根挂在 /workspace/out，应用侧路径需按相对位置换算
        （例如 /srv/airclaw-out/tmp/run-x → /workspace/out/tmp/run-x）。

        调用方据此拼出容器内要执行的文件路径，否则脚本会「找不到」。
        """
        settings = self._settings
        if not settings.sandbox_uses_volume:
            return SANDBOX_OUT
        root = Path(settings.sandbox_out_mount).resolve()
        try:
            rel = Path(app_path).resolve().relative_to(root)
        except ValueError:
            raise SandboxError(
                f"输出目录 {app_path} 不在输出卷挂载点 {root} 之内，"
                f"沙箱容器无法访问。请检查 SANDBOX_OUT_MOUNT 配置。"
            ) from None
        return f"{SANDBOX_OUT}/{rel.as_posix()}" if rel.parts else SANDBOX_OUT

    @staticmethod
    def _logs(container, *, stdout: bool, stderr: bool) -> str:
        raw = container.logs(stdout=stdout, stderr=stderr)
        return raw.decode("utf-8", errors="replace").strip()
