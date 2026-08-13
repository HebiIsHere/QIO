# -*- coding: utf-8 -*-
"""实体识别器：私密绑定模式提取 + 经验性判定。"""
from agent.entities.recognizer import (
    GENERIC_OBJECTS,
    extract_bound_objects,
    is_entity_candidate,
)


def test_ownership_binding():
    objs = extract_bound_objects("我家的鹅最近嘴巴红肿了")
    assert "鹅" in objs


def test_preference_binding():
    objs = extract_bound_objects("错了错了，我喜欢吃大盘的肥羊")
    assert any("肥羊" in o for o in objs)


def test_kinship_binding():
    objs = extract_bound_objects("你说我那个师哥是不是有病")
    assert any("师哥" in o for o in objs)


def test_place_binding():
    objs = extract_bound_objects("我要去我们实验室做实验")
    assert any("实验室" in o for o in objs)


def test_generic_object_excluded():
    objs = extract_bound_objects("我爱吃火锅")
    # 通用类别「火锅」不应成为候选（需唯一指称/私有属性）
    assert not any(o == "火锅" for o in objs)
    assert "火锅" in GENERIC_OBJECTS


def test_high_frequency_with_attributes_qualifies():
    assert is_entity_candidate("我家的鹅", mention_count=4, has_attributes=True, bound_hit=False)
    assert not is_entity_candidate("鹅", mention_count=1, has_attributes=False, bound_hit=False)


def test_generic_never_qualifies():
    assert not is_entity_candidate("火锅", mention_count=99, has_attributes=True, bound_hit=True)
