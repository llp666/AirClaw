# AirClaw 代码执行沙箱
#
# 用途：terminal / python_repl / code_test 技能的一切代码执行动作，统一在本镜像内完成。
#       宿主机不得直接运行 Agent 生成的任何指令。
#
# 构建（需在联网机器上构建后导出，或在内网私有镜像源构建）：
#   docker build -f backend/sandbox/Dockerfile -t airclaw-sandbox:latest .
#   离线导入：docker save airclaw-sandbox:latest -o airclaw-sandbox.tar
#
#   注意构建上下文是**仓库根目录**而非 backend/sandbox/：镜像要装被测代码的
#   依赖，需要 COPY 到 backend/requirements.lock.txt。根目录下的无关内容由
#   同目录的 Dockerfile.dockerignore 排除。
#
# 运行（由 backend/tools/ 下的工具实现调用，参数不可由 Agent 修改）：
#   docker run --rm --network=none \
#     --cpus=2 --memory=2g --pids-limit=256 \
#     --read-only --tmpfs /tmp:rw,size=512m \
#     -v <被测代码目录>:/workspace/src:ro \
#     -v <输出目录>:/workspace/out:rw \
#     --user 1000:1000 \
#     airclaw-sandbox:latest \
#     python -m pytest /workspace/src/backend/tests/test_x.py -v
#
# 隔离要点（对应 PRD 第八章）：
#   --network=none   无网络模式，杜绝借道容器发起任何外联
#   --read-only      根文件系统只读，仅 /tmp 与挂载的输出目录可写
#   --user 1000:1000 非 root 运行
#   不注入任何宿主机密钥、凭证与环境变量

FROM python:3.13-slim

# ---------------------------------------------------------------- 被测代码的运行时依赖
#
# 为什么要装应用依赖：code_test 技能的测试对象是本项目的模块（如
# backend/tools/terminal_tool.py），它们 import langchain_core / pydantic 等。
# 只装 pytest 的话，测试文件第一步 import 就会 ModuleNotFoundError，
# 技能等于形同虚设——实测确认过。
#
# 用 requirements.lock.txt 而非 requirements.txt，是为了让沙箱环境与应用环境
# 是同一套精确版本：测试跑在一个和线上不同的依赖组合上，结论没有意义。
#
# 代价是镜像明显变大（含 numpy / scipy / pandas / llama-index-core），
# 离线交付包因此增加数百 MB。这是让技能可用的必要成本。
COPY backend/requirements.lock.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

# ---------------------------------------------------------------- 测试工具链
#
# lock 是应用运行期依赖的冻结结果，不含测试工具，故单独安装。
RUN pip install --no-cache-dir pytest pytest-cov

# 非 root 运行。与运行时 --user 1000:1000 对应。
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin sandbox

# ---------------------------------------------------------------- 技能执行封装
#
# code_test 技能用的 pytest 封装。裸命令在只读工作区里需要一整套非默认参数
# 才能跑通，固定进镜像后，技能说明书里只剩一句调用，模型不必复现长命令。
# 放在最后：脚本改动不会让上面的 pip 层失效。
COPY backend/sandbox/run_tests.sh /usr/local/bin/airclaw-test
RUN chmod 0755 /usr/local/bin/airclaw-test

# 工作区约定：
#   /workspace/src  被测代码（只读挂载，含被测模块与测试用例）
#   /workspace/out  输出目录（读写挂载，pytest 日志与覆盖率数据落在这里）
# 没有 /workspace/tests：测试用例随项目一起在 src 内，不单独挂载。
RUN mkdir -p /workspace/src /workspace/out \
    && chown -R 1000:1000 /workspace

WORKDIR /workspace

# 禁止 Python 写出 .pyc，避免只读根文件系统下报错
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=0 \
    PIP_NO_INDEX=1

USER 1000:1000

# 无 ENTRYPOINT/CMD 固定值：每次执行由工具层传入具体命令，
# 默认给出一个拒绝执行的占位命令，防止误用镜像时的隐式行为。
CMD ["python", "-c", "import sys; sys.stderr.write('airclaw-sandbox: no command provided\n'); sys.exit(1)"]
