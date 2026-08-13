# -*- coding: utf-8 -*-
"""实体识别器：私密绑定模式（规则层） + 经验性判定。

设计（对应实体卡 v1）：
- 规则层只做「候选」：用私密绑定模式从文本里低成本提取「用户私人世界的对象」，
  不负责最终写入（写入由主模型封块时确认，符合写入纪律）。
- 经验性判定：绑定命中（且非通用词）是强信号；或 高频提及 + 已被提炼过属性。
- 通用词（火锅/人/电脑…）无私有绑定，一律排除。
"""
from __future__ import annotations

import re

# 通用类别词：无私有绑定、不应成为经验性实体
GENERIC_OBJECTS = {
    "人", "火锅", "电脑", "手机", "游戏", "电影", "音乐", "书", "菜", "饭",
    "动物", "狗", "猫", "车", "房子", "工作", "学习", "运动", "旅行",
}

# 尾助词/语气词：清洗对象名时去掉
_TRAILING = set("的了着吧啊呢呀哈吗嘛哦")

# 属性/状态词：领属/饲养模式提取的对象名在遇到这些词时截断（实体名不应带属性描述）
_ATTRIBUTE_MARKERS = (
    "最近", "现在", "今天", "昨天", "已经", "有点", "突然", "正在", "一直",
    "嘴巴", "生病", "红肿", "受伤", "买了", "养了", "要", "很", "不", "准备",
)

# 私密绑定模式（领属 / 饲养 / 喜好 / 亲缘人际 / 处所组织）
_BINDING_PATTERNS = [
    re.compile(r"(?:我家的?|我家|我的)(?P<obj>[^，。！？、\s,，]{1,10})"),
    re.compile(r"我(?:家里)?养(?:了|着|的)?(?P<obj>[^，。！？、\s,，]{1,10})"),
    re.compile(r"我(?:最)?(?:喜欢|爱|爱吃|偏爱)(?:吃|喝|用|玩|去)?(?P<obj>[^，。！？、\s,，]{1,12})"),
    re.compile(r"我(?:那个|这个)?(?:的)?(?P<obj>师哥|师兄|妈妈|爸爸|弟弟|妹妹|哥哥|姐姐|朋友|同事|同学|领导|老师|对象|老婆|老公)"),
    re.compile(r"我们(?P<obj>公司|实验室|学校|单位|宿舍)"),
]


def _clean(name: str) -> str:
    name = name.strip().strip("，。！？、 ")
    while name and name[-1] in _TRAILING:
        name = name[:-1]
    # 属性/状态词处截断：实体名是名词短语，不含属性描述
    for marker in _ATTRIBUTE_MARKERS:
        i = name.find(marker)
        if i > 0:
            name = name[:i]
            break
    return name.strip()


def extract_bound_objects(text: str) -> list[str]:
    """用私密绑定模式提取候选对象名（已清洗、去通用词、去重）。"""
    out: list[str] = []
    for pattern in _BINDING_PATTERNS:
        for match in pattern.finditer(text):
            obj = _clean(match.group("obj") or "")
            if not obj or obj in GENERIC_OBJECTS:
                continue
            if obj not in out:
                out.append(obj)
    return out


def is_entity_candidate(
    name: str,
    mention_count: int,
    has_attributes: bool = False,
    bound_hit: bool = False,
    *,
    threshold: int = 3,
) -> bool:
    """经验性判定：通用词永不收；绑定命中是强信号；或高频 + 已被提炼属性。"""
    name = (name or "").strip()
    if not name or name in GENERIC_OBJECTS:
        return False
    if bound_hit:
        return True
    return mention_count >= threshold and has_attributes
