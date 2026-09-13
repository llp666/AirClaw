"""skills_scanner —— 技能扫描器。

不是工具，而是启动时执行的一次性扫描：遍历 skills/*/SKILL.md，解析 YAML frontmatter，
生成 SKILLS_SNAPSHOT.md。该快照被拼进 System Prompt，让 Agent 知道有哪些可用技能、
以及每个技能的说明书在哪。

对应 PRD 第三章 2.1「Agent Skills 读取流程 (Bootstrap)」：

    <available_skills>
      <skill>
        <name>code_test</name>
        <description>为指定 Python 模块生成单元测试用例、执行测试并输出测试报告</description>
        <location>./backend/skills/code_test/SKILL.md</location>
      </skill>
    </available_skills>

location 使用相对路径，Agent 会用它调用 read_file。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

logger = logging.getLogger("airclaw.skills")

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

HEADER = """<!-- 本文件由 tools/skills_scanner.py 在服务启动时自动生成，请勿手工编辑。 -->
<!-- 它是 System Prompt 的第 2 个组件（第 1 个是运行时上下文），告知 Agent 有哪些可用技能及其说明书位置。 -->

<available_skills>
"""

FOOTER = """
</available_skills>
"""


def _parse_frontmatter(text: str) -> dict:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        logger.warning("SKILL.md frontmatter YAML 解析失败：%s", exc)
        return {}
    return data if isinstance(data, dict) else {}


def scan_skills(skills_dir: Path, output_path: Path, project_root: Path) -> list[dict]:
    """扫描技能目录并写出 SKILLS_SNAPSHOT.md。

    返回解析到的技能列表，便于启动日志汇总与测试断言。
    """
    skills: list[dict] = []

    if skills_dir.is_dir():
        for skill_dir in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.is_file():
                continue

            meta = _parse_frontmatter(skill_md.read_text(encoding="utf-8"))
            # 目录名作为稳定的技能标识；frontmatter 里的 name 是可读标题
            skills.append(
                {
                    "id": skill_dir.name,
                    "title": str(meta.get("name", skill_dir.name)),
                    "description": str(meta.get("description", "")).strip(),
                    "path": skill_md,
                }
            )

    lines = [HEADER]
    for skill in skills:
        try:
            location = "./" + skill["path"].relative_to(project_root).as_posix()
        except ValueError:
            # 技能目录不在项目根下（例如被配置到外部路径），退回绝对路径
            location = skill["path"].as_posix()

        lines.append("  <skill>\n")
        lines.append(f"    <name>{skill['id']}</name>\n")
        lines.append(f"    <description>{skill['description']}</description>\n")
        lines.append(f"    <location>{location}</location>\n")
        lines.append("  </skill>\n")
    lines.append(FOOTER)

    output_path.write_text("".join(lines), encoding="utf-8")
    logger.info("技能扫描完成：发现 %d 个技能，快照已写入 %s", len(skills), output_path.name)
    return skills
